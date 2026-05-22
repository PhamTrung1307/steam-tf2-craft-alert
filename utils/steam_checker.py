from dataclasses import dataclass
from datetime import datetime
import html
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup


NOT_CRAFTABLE_PATTERNS = (
    re.compile(r"\(?\s*not\s+usable\s+in\s+crafting\s*\)?", re.IGNORECASE),
    re.compile(r"cannot\s+be\s+used\s+in\s+crafting", re.IGNORECASE),
    re.compile(r"not\s+usable", re.IGNORECASE),
)

ANTI_BOT_PATTERNS = (
    re.compile(r"access\s+denied", re.IGNORECASE),
    re.compile(r"captcha", re.IGNORECASE),
    re.compile(r"verify\s+you\s+are\s+human", re.IGNORECASE),
    re.compile(r"unusual\s+traffic", re.IGNORECASE),
    re.compile(r"automated\s+access", re.IGNORECASE),
    re.compile(r"temporarily\s+unable", re.IGNORECASE),
)

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": CHROME_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://steamcommunity.com/market/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
}


@dataclass
class SteamCheckResult:
    url: str
    item_name: str
    status: str
    checked_at: str
    http_status: Optional[int] = None
    error: Optional[str] = None
    found_not_craftable_pattern: bool = False
    matched_text: Optional[str] = None
    description_html: Optional[str] = None
    description_text: Optional[str] = None
    parsed_json: Optional[Dict[str, Any]] = None
    anti_bot_detected: bool = False
    raw_html: Optional[str] = None


def now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_item_name(url: str) -> str:
    try:
        path = urlparse(url).path
        item_slug = path.rstrip("/").split("/")[-1]
        return unquote(item_slug).strip() or url
    except Exception:
        return url


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    return session


def normalize_html(value: str) -> str:
    decoded = html.unescape(value or "")
    decoded = decoded.lower()
    decoded = decoded.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    decoded = re.sub(r"\s+", " ", decoded)
    return decoded.strip()


def normalize_description(value: str) -> str:
    decoded = html.unescape(value or "")
    if "<" in decoded and ">" in decoded:
        soup = BeautifulSoup(decoded, "html.parser")
        text = soup.get_text(" ", strip=True)
    else:
        text = decoded
    normalized = html.unescape(text or value or "").lower()
    normalized = normalized.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def html_to_searchable_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return html_to_single_line(soup.get_text(" ", strip=True))


def html_to_single_line(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def extract_description_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    blocks: List[str] = []

    selectors = [
        ".market_listing_item_descriptors",
        ".descriptor",
        ".market_listing_largeimage",
        "#largeiteminfo_item_descriptors",
        "#largeiteminfo",
        "#hover_item_descriptors",
    ]
    for selector in selectors:
        for element in soup.select(selector):
            rendered = str(element).strip()
            if rendered and rendered not in blocks:
                blocks.append(rendered)

    if not blocks:
        normalized_raw = html_to_single_line(raw_html)
        match = re.search(
            r".{0,700}(not\s+usable|crafting|descriptor|item_descriptors).{0,1200}",
            normalized_raw,
            re.IGNORECASE,
        )
        if match:
            blocks.append(match.group(0))

    return "\n\n".join(blocks)


def extract_json_after_marker(raw_html: str, marker: str) -> Optional[Dict[str, Any]]:
    marker_index = raw_html.lower().find(marker.lower())
    if marker_index == -1:
        return None

    after_marker = raw_html[marker_index:]
    parse_call_match = re.search(r"json\.parse\s*\(", after_marker, re.IGNORECASE)
    if parse_call_match:
        string_start = parse_call_match.end()
        decoder = json.JSONDecoder()
        try:
            encoded_json, _end = decoder.raw_decode(after_marker[string_start:])
        except json.JSONDecodeError:
            return None
        if not isinstance(encoded_json, str):
            return None
        try:
            parsed = json.loads(encoded_json)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    start_index = raw_html.find("{", marker_index)
    if start_index == -1:
        return None

    decoder = json.JSONDecoder()
    try:
        parsed, _end = decoder.raw_decode(raw_html[start_index:])
    except json.JSONDecodeError:
        return None

    return parsed if isinstance(parsed, dict) else None


def parse_ssr_render_context(raw_html: str) -> Optional[Dict[str, Any]]:
    markers = (
        "window.SSR.renderContext",
        "SSR.renderContext",
    )
    for marker in markers:
        parsed = extract_json_after_marker(raw_html, marker)
        if parsed is not None:
            return parsed
    return None


def collect_strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: List[str] = []
        for nested in value.values():
            strings.extend(collect_strings(nested))
        return strings
    if isinstance(value, list):
        strings = []
        for nested in value:
            strings.extend(collect_strings(nested))
        return strings
    return []


def parse_json_string(value: str) -> Optional[Any]:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def collect_description_values(value: Any) -> List[str]:
    descriptions: List[str] = []

    if isinstance(value, dict):
        for key, nested in value.items():
            lowered_key = str(key).lower()
            if lowered_key in {"description", "descriptions"}:
                descriptions.extend(collect_strings(nested))
            else:
                descriptions.extend(collect_description_values(nested))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                parsed = parse_json_string(item)
                if parsed is None:
                    descriptions.append(item)
                else:
                    descriptions.extend(collect_description_values(parsed))
            elif isinstance(item, (dict, list)):
                descriptions.extend(collect_description_values(item))
    elif isinstance(value, str):
        parsed = parse_json_string(value)
        if parsed is not None:
            descriptions.extend(collect_description_values(parsed))

    return descriptions


def extract_parsed_description_text(parsed_json: Dict[str, Any]) -> str:
    values = collect_description_values(parsed_json)
    normalized_values = [normalize_description(value) for value in values]
    return " ".join(value for value in normalized_values if value).strip()


def find_not_craftable_match(raw_html: str) -> Tuple[bool, Optional[str]]:
    text = html_to_searchable_text(raw_html)
    combined = "\n".join(
        [
            raw_html,
            html.unescape(raw_html),
            text,
            extract_description_html(raw_html),
        ]
    )
    normalized = normalize_html(combined)

    for pattern in NOT_CRAFTABLE_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return True, match.group(0)

    return False, None


def find_not_craftable_in_text(text: str) -> Tuple[bool, Optional[str]]:
    normalized = normalize_description(text)

    for pattern in NOT_CRAFTABLE_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return True, match.group(0)

    return False, None


def is_anti_bot_page(raw_html: str) -> bool:
    normalized = normalize_html(raw_html)
    return any(pattern.search(normalized) for pattern in ANTI_BOT_PATTERNS)


def detect_status(
    raw_html: str,
) -> Tuple[str, bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    parsed_json = parse_ssr_render_context(raw_html)
    if parsed_json is not None:
        description_text = extract_parsed_description_text(parsed_json)
        if not description_text:
            return "UNKNOWN", False, None, parsed_json, None

        found, matched_text = find_not_craftable_in_text(description_text)
        if found:
            return "NOT_CRAFTABLE", True, matched_text, parsed_json, description_text

        return "CRAFTABLE", False, None, parsed_json, description_text

    found, matched_text = find_not_craftable_match(raw_html)
    if found:
        return "NOT_CRAFTABLE", True, matched_text, None, normalize_description(extract_description_html(raw_html))

    return "UNKNOWN", False, None, None, normalize_description(extract_description_html(raw_html))


def check_market_item(
    url: str,
    timeout: int = 20,
    session: Optional[requests.Session] = None,
    retries: int = 3,
    include_html: bool = False,
) -> SteamCheckResult:
    item_name = parse_item_name(url)
    checked_at = now_string()
    active_session = session or create_session()
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = active_session.get(url, timeout=timeout)
        except requests.Timeout:
            last_error = f"Timeout after {timeout} seconds"
        except requests.RequestException as exc:
            last_error = f"Network error: {exc}"
        else:
            break

        if attempt < retries:
            time.sleep(attempt)
    else:
        return SteamCheckResult(
            url=url,
            item_name=item_name,
            status="ERROR",
            checked_at=checked_at,
            error=last_error or "Request failed",
        )

    anti_bot_detected = is_anti_bot_page(response.text)
    description_html = extract_description_html(response.text)
    status, found, matched_text, parsed_json, description_text = (
        detect_status(response.text)
        if response.text.strip()
        else ("ERROR", False, None, None, None)
    )

    if response.status_code == 429:
        return SteamCheckResult(
            url=url,
            item_name=item_name,
            status="ERROR",
            checked_at=checked_at,
            http_status=response.status_code,
            error="Steam rate limit HTTP 429",
            description_html=description_html,
            description_text=description_text,
            parsed_json=parsed_json if include_html else None,
            anti_bot_detected=anti_bot_detected,
            raw_html=response.text if include_html else None,
        )

    if response.status_code != 200:
        return SteamCheckResult(
            url=url,
            item_name=item_name,
            status="ERROR",
            checked_at=checked_at,
            http_status=response.status_code,
            error=f"Unexpected HTTP status {response.status_code}",
            description_html=description_html,
            description_text=description_text,
            parsed_json=parsed_json if include_html else None,
            anti_bot_detected=anti_bot_detected,
            raw_html=response.text if include_html else None,
        )

    if not response.text.strip():
        return SteamCheckResult(
            url=url,
            item_name=item_name,
            status="ERROR",
            checked_at=checked_at,
            http_status=response.status_code,
            error="Empty HTML response",
            description_html=description_html,
            description_text=description_text,
            parsed_json=parsed_json if include_html else None,
            anti_bot_detected=anti_bot_detected,
            raw_html=response.text if include_html else None,
        )

    if anti_bot_detected:
        return SteamCheckResult(
            url=url,
            item_name=item_name,
            status="ERROR",
            checked_at=checked_at,
            http_status=response.status_code,
            error="Possible Steam anti-bot page detected",
            description_html=description_html,
            description_text=description_text,
            parsed_json=parsed_json if include_html else None,
            anti_bot_detected=True,
            raw_html=response.text if include_html else None,
        )

    return SteamCheckResult(
        url=url,
        item_name=item_name,
        status=status,
        checked_at=checked_at,
        http_status=response.status_code,
        found_not_craftable_pattern=found,
        matched_text=matched_text,
        description_html=description_html,
        description_text=description_text,
        parsed_json=parsed_json if include_html else None,
        raw_html=response.text if include_html else None,
    )
