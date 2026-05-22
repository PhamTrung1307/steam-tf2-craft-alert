from dataclasses import dataclass
from datetime import datetime
import html
import re
import time
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup


NOT_CRAFTABLE_PATTERNS = (
    re.compile(r"\(?\s*not\s+usable\s+in\s+crafting\s*\)?", re.IGNORECASE),
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


def is_anti_bot_page(raw_html: str) -> bool:
    normalized = normalize_html(raw_html)
    return any(pattern.search(normalized) for pattern in ANTI_BOT_PATTERNS)


def detect_status(raw_html: str) -> Tuple[str, bool, Optional[str]]:
    found, matched_text = find_not_craftable_match(raw_html)
    if found:
        return "NOT_CRAFTABLE", True, matched_text

    return "CRAFTABLE", False, None


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

    description_html = extract_description_html(response.text)
    anti_bot_detected = is_anti_bot_page(response.text)
    status, found, matched_text = detect_status(response.text) if response.text.strip() else ("ERROR", False, None)

    if found:
        return SteamCheckResult(
            url=url,
            item_name=item_name,
            status=status,
            checked_at=checked_at,
            http_status=response.status_code,
            found_not_craftable_pattern=found,
            matched_text=matched_text,
            description_html=description_html,
            anti_bot_detected=anti_bot_detected,
            raw_html=response.text if include_html else None,
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
        raw_html=response.text if include_html else None,
    )
