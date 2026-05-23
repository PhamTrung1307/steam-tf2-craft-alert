from dataclasses import dataclass, field
from datetime import datetime
import html
import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup


NOT_CRAFTABLE_PATTERNS = (
    re.compile(r"\(?\s*not\s+usable\s+in\s+crafting\s*\)?", re.IGNORECASE),
    re.compile(r"cannot\s+be\s+used\s+in\s+crafting", re.IGNORECASE),
    re.compile(r"\bnot\s+usable\b", re.IGNORECASE),
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
class ParserResult:
    status: str
    parse_method: str
    used_layout: str
    parsed_item_name: Optional[str] = None
    appid: Optional[int] = None
    found_not_craftable: bool = False
    matched_patterns: List[str] = field(default_factory=list)
    extracted_texts: List[str] = field(default_factory=list)
    parsed_json: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


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
    matched_patterns: List[str] = field(default_factory=list)
    description_html: Optional[str] = None
    description_text: Optional[str] = None
    extracted_texts: List[str] = field(default_factory=list)
    parsed_json: Optional[Dict[str, Any]] = None
    parsed_item_name: Optional[str] = None
    appid: Optional[int] = None
    parse_method: str = "parser_failed"
    used_layout: str = "unknown"
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


def parse_url_appid(url: str) -> Optional[int]:
    parts = [part for part in urlparse(url).path.split("/") if part]
    try:
        listings_index = parts.index("listings")
        return int(parts[listings_index + 1])
    except (ValueError, IndexError):
        return None


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    return session


def decode_backslash_escapes(value: str) -> str:
    text = value or ""
    for _ in range(2):
        if not re.search(r"\\(?:u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|/|n|r|t|\"|')", text):
            break
        try:
            decoded = bytes(text, "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            break
        if decoded == text:
            break
        text = decoded
    return text


def normalize_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = decode_backslash_escapes(text)
    text = html.unescape(text)
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    text = html.unescape(text).lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def names_match(candidate: Any, expected_item_name: str) -> bool:
    normalized_candidate = normalize_text(candidate)
    normalized_expected = normalize_text(expected_item_name)
    return bool(normalized_candidate and normalized_candidate == normalized_expected)


def html_to_searchable_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    return re.sub(r"\s+", " ", html.unescape(soup.get_text(" ", strip=True))).strip()


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
        "#largeiteminfo_react_placeholder",
    ]
    for selector in selectors:
        for element in soup.select(selector):
            rendered = str(element).strip()
            if rendered and rendered not in blocks:
                blocks.append(rendered)

    if not blocks:
        normalized_raw = re.sub(r"\s+", " ", html.unescape(raw_html))
        match = re.search(
            r".{0,700}(not\s+usable|crafting|descriptor|largeiteminfo).{0,1200}",
            normalized_raw,
            re.IGNORECASE,
        )
        if match:
            blocks.append(match.group(0))

    return "\n\n".join(blocks)


def parse_json_string(value: str) -> Optional[Any]:
    stripped = html.unescape(value or "").strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def find_balanced_object(text: str, start_index: int) -> Optional[str]:
    brace_start = text.find("{", start_index)
    if brace_start == -1:
        return None

    depth = 0
    in_string: Optional[str] = None
    escape = False
    for index in range(brace_start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == in_string:
                in_string = None
            continue

        if char in {"'", '"'}:
            in_string = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start : index + 1]
    return None


def js_object_to_json(value: str) -> Optional[Any]:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass

    # Steam's globals are normally JSON object literals; this handles the small
    # amount of JavaScript syntax that appears in older market pages.
    sanitized = re.sub(r",\s*([}\]])", r"\1", value)
    sanitized = re.sub(r"(?<!\\)'", '"', sanitized)
    try:
        return json.loads(sanitized)
    except json.JSONDecodeError:
        return None


def extract_js_global_object(raw_html: str, variable_name: str) -> Optional[Dict[str, Any]]:
    match = re.search(rf"\bvar\s+{re.escape(variable_name)}\s*=", raw_html)
    if not match:
        return None
    object_text = find_balanced_object(raw_html, match.end())
    if not object_text:
        return None
    parsed = js_object_to_json(object_text)
    return parsed if isinstance(parsed, dict) else None


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

    object_text = find_balanced_object(raw_html, marker_index)
    if not object_text:
        return None
    parsed = js_object_to_json(object_text)
    return parsed if isinstance(parsed, dict) else None


def parse_render_context(raw_html: str) -> Optional[Dict[str, Any]]:
    for marker in ("window.SSR.renderContext", "SSR.renderContext"):
        parsed = extract_json_after_marker(raw_html, marker)
        if parsed is not None:
            return parsed
    return None


def coerce_query_data(render_context: Dict[str, Any]) -> Any:
    query_data = render_context.get("queryData")
    if isinstance(query_data, str):
        parsed = parse_json_string(query_data)
        return parsed if parsed is not None else query_data
    return query_data


def iter_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from iter_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_dicts(nested)
    elif isinstance(value, str):
        parsed = parse_json_string(value)
        if parsed is not None:
            yield from iter_dicts(parsed)


def collect_description_texts(item: Dict[str, Any]) -> List[str]:
    fields = ("descriptions", "owner_descriptions", "fraudwarnings")
    texts: List[str] = []
    for field_name in fields:
        value = item.get(field_name)
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    raw = entry.get("value")
                    if raw is not None:
                        texts.append(normalize_text(raw))
                elif entry is not None:
                    texts.append(normalize_text(entry))
        elif value is not None:
            texts.append(normalize_text(value))

    return [text for text in texts if text]


def collect_item_texts(item: Dict[str, Any]) -> List[str]:
    texts = collect_description_texts(item)
    for field_name in ("name", "market_name", "market_hash_name", "type"):
        value = item.get(field_name)
        if value:
            texts.append(normalize_text(value))
    return [text for text in texts if text]


def item_appid(item: Dict[str, Any]) -> Optional[int]:
    for key in ("appid", "app_id"):
        value = item.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def item_name_matches(item: Dict[str, Any], expected_item_name: str) -> bool:
    return any(
        names_match(item.get(field_name), expected_item_name)
        for field_name in ("market_hash_name", "market_name", "name")
    )


def evaluate_parsed_item(
    item: Dict[str, Any],
    expected_item_name: str,
    parse_method: str,
    used_layout: str,
    parsed_json: Optional[Dict[str, Any]] = None,
) -> ParserResult:
    description_texts = collect_description_texts(item)
    texts = collect_item_texts(item)
    found, matches = find_not_craftable_in_texts(texts)
    appid = item_appid(item)
    parsed_item_name = (
        item.get("market_hash_name") or item.get("market_name") or item.get("name")
    )

    if found:
        return ParserResult(
            status="NOT_CRAFTABLE",
            parse_method=parse_method,
            used_layout=used_layout,
            parsed_item_name=str(parsed_item_name) if parsed_item_name else None,
            appid=appid,
            found_not_craftable=True,
            matched_patterns=matches,
            extracted_texts=texts,
            parsed_json=parsed_json,
        )

    if appid == 440 and item_name_matches(item, expected_item_name) and description_texts:
        return ParserResult(
            status="CRAFTABLE",
            parse_method=parse_method,
            used_layout=used_layout,
            parsed_item_name=str(parsed_item_name) if parsed_item_name else None,
            appid=appid,
            extracted_texts=texts,
            parsed_json=parsed_json,
        )

    return ParserResult(
        status="UNKNOWN",
        parse_method=parse_method,
        used_layout=used_layout,
        parsed_item_name=str(parsed_item_name) if parsed_item_name else None,
        appid=appid,
        extracted_texts=texts,
        parsed_json=parsed_json,
        error="Parsed item did not satisfy appid/name/text requirements",
    )


def parse_beta_render_context(raw_html: str, expected_item_name: str) -> ParserResult:
    render_context = parse_render_context(raw_html)
    if render_context is None:
        return ParserResult("UNKNOWN", "parser_failed", "unknown", error="No renderContext")

    query_data = coerce_query_data(render_context)
    candidates: List[Dict[str, Any]] = []
    for candidate in iter_dicts(query_data):
        if (
            item_appid(candidate) == 440
            and item_name_matches(candidate, expected_item_name)
            and any(key in candidate for key in ("descriptions", "owner_descriptions", "fraudwarnings"))
        ):
            candidates.append(candidate)

    if not candidates:
        for candidate in iter_dicts(query_data):
            description = candidate.get("description")
            if isinstance(description, dict) and item_name_matches(description, expected_item_name):
                enriched = dict(description)
                if "appid" not in enriched and item_appid(candidate) is not None:
                    enriched["appid"] = item_appid(candidate)
                candidates.append(enriched)

    for candidate in candidates:
        result = evaluate_parsed_item(
            candidate,
            expected_item_name,
            parse_method="beta:renderContext",
            used_layout="beta",
            parsed_json=render_context,
        )
        if result.status in {"CRAFTABLE", "NOT_CRAFTABLE"}:
            return result

    return ParserResult(
        status="UNKNOWN",
        parse_method="beta:renderContext",
        used_layout="beta",
        parsed_json=render_context,
        error="No matching TF2 item description found",
    )


def get_classic_assets(raw_html: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    assets = extract_js_global_object(raw_html, "g_rgAssets")
    listing_info = extract_js_global_object(raw_html, "g_rgListingInfo")
    return assets, listing_info


def find_asset_by_id(assets: Dict[str, Any], appid: Any, contextid: Any, assetid: Any) -> Optional[Dict[str, Any]]:
    app_assets = assets.get(str(appid)) or assets.get(appid)
    if not isinstance(app_assets, dict):
        return None
    context_assets = app_assets.get(str(contextid)) or app_assets.get(contextid)
    if not isinstance(context_assets, dict):
        return None
    asset = context_assets.get(str(assetid)) or context_assets.get(assetid)
    return asset if isinstance(asset, dict) else None


def iter_classic_appid_440_assets(assets: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    app_assets = assets.get("440") or assets.get(440)
    if not isinstance(app_assets, dict):
        return
    for context_assets in app_assets.values():
        if not isinstance(context_assets, dict):
            continue
        for asset in context_assets.values():
            if isinstance(asset, dict):
                enriched = dict(asset)
                enriched.setdefault("appid", 440)
                yield enriched


def listing_assets(listing_info: Optional[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    if not isinstance(listing_info, dict):
        return
    for listing in listing_info.values():
        if not isinstance(listing, dict):
            continue
        asset = listing.get("asset")
        if isinstance(asset, dict):
            yield asset


def parse_classic_g_rgAssets(raw_html: str, expected_item_name: str) -> ParserResult:
    assets, listing_info = get_classic_assets(raw_html)
    if assets is None:
        return ParserResult("UNKNOWN", "parser_failed", "unknown", error="No g_rgAssets")

    candidates: List[Dict[str, Any]] = []
    seen_ids = set()
    for listing_asset in listing_assets(listing_info):
        asset = find_asset_by_id(
            assets,
            listing_asset.get("appid"),
            listing_asset.get("contextid"),
            listing_asset.get("id"),
        )
        if asset and id(asset) not in seen_ids:
            enriched = dict(asset)
            enriched.setdefault("appid", 440)
            candidates.append(enriched)
            seen_ids.add(id(asset))

    if not candidates:
        candidates.extend(iter_classic_appid_440_assets(assets))

    matching_candidates = [
        candidate for candidate in candidates if item_name_matches(candidate, expected_item_name)
    ]
    if not matching_candidates:
        return ParserResult(
            status="UNKNOWN",
            parse_method="classic:g_rgAssets",
            used_layout="classic",
            error="No matching market name in g_rgAssets",
        )

    for candidate in matching_candidates:
        result = evaluate_parsed_item(
            candidate,
            expected_item_name,
            parse_method="classic:g_rgAssets",
            used_layout="classic",
        )
        if result.status in {"CRAFTABLE", "NOT_CRAFTABLE"}:
            return result

    return ParserResult(
        status="UNKNOWN",
        parse_method="classic:g_rgAssets",
        used_layout="classic",
        extracted_texts=collect_description_texts(matching_candidates[0]),
        error="Matching classic asset had no usable descriptions",
    )


def find_not_craftable_in_texts(texts: Iterable[str]) -> Tuple[bool, List[str]]:
    matches: List[str] = []
    for text in texts:
        normalized = normalize_text(text)
        for pattern in NOT_CRAFTABLE_PATTERNS:
            match = pattern.search(normalized)
            if match:
                matches.append(match.group(0))
    return bool(matches), matches


def fallback_raw_scan(raw_html: str) -> ParserResult:
    description_html = extract_description_html(raw_html)
    texts = [
        normalize_text(description_html),
        normalize_text(html_to_searchable_text(raw_html)),
        normalize_text(raw_html),
    ]
    found, matches = find_not_craftable_in_texts(texts)
    if found:
        return ParserResult(
            status="NOT_CRAFTABLE",
            parse_method="fallback_raw_scan",
            used_layout="unknown",
            found_not_craftable=True,
            matched_patterns=matches,
            extracted_texts=[text for text in texts if text],
        )
    return ParserResult(
        status="UNKNOWN",
        parse_method="parser_failed",
        used_layout="unknown",
        extracted_texts=[text for text in texts if text],
        error="Beta and classic parsers failed",
    )


def detect_status(raw_html: str, expected_item_name: str) -> ParserResult:
    beta_result = parse_beta_render_context(raw_html, expected_item_name)
    if beta_result.used_layout == "beta" and beta_result.status in {"CRAFTABLE", "NOT_CRAFTABLE"}:
        return beta_result

    classic_result = parse_classic_g_rgAssets(raw_html, expected_item_name)
    if classic_result.used_layout == "classic" and classic_result.status in {"CRAFTABLE", "NOT_CRAFTABLE"}:
        return classic_result

    fallback_result = fallback_raw_scan(raw_html)
    if fallback_result.status == "NOT_CRAFTABLE":
        return fallback_result

    if beta_result.used_layout == "beta":
        return beta_result
    if classic_result.used_layout == "classic":
        return classic_result
    return fallback_result


def is_anti_bot_page(raw_html: str) -> bool:
    normalized = normalize_text(raw_html)
    return any(pattern.search(normalized) for pattern in ANTI_BOT_PATTERNS)


def result_from_parser(
    url: str,
    item_name: str,
    checked_at: str,
    parser_result: ParserResult,
    http_status: Optional[int],
    raw_html: str,
    include_html: bool,
    anti_bot_detected: bool,
    error: Optional[str] = None,
) -> SteamCheckResult:
    description_html = extract_description_html(raw_html)
    description_text = "\n".join(parser_result.extracted_texts).strip()
    return SteamCheckResult(
        url=url,
        item_name=item_name,
        status=parser_result.status,
        checked_at=checked_at,
        http_status=http_status,
        error=error or parser_result.error,
        found_not_craftable_pattern=parser_result.found_not_craftable,
        matched_text=parser_result.matched_patterns[0] if parser_result.matched_patterns else None,
        matched_patterns=parser_result.matched_patterns,
        description_html=description_html,
        description_text=description_text,
        extracted_texts=parser_result.extracted_texts,
        parsed_json=parser_result.parsed_json if include_html else None,
        parsed_item_name=parser_result.parsed_item_name,
        appid=parser_result.appid,
        parse_method=parser_result.parse_method,
        used_layout=parser_result.used_layout,
        anti_bot_detected=anti_bot_detected,
        raw_html=raw_html if include_html else None,
    )


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

    raw_html = response.text or ""
    anti_bot_detected = is_anti_bot_page(raw_html)
    parser_result = (
        detect_status(raw_html, item_name)
        if raw_html.strip()
        else ParserResult("ERROR", "parser_failed", "unknown", error="Empty HTML response")
    )

    if response.status_code == 429:
        parser_result.status = "ERROR"
        return result_from_parser(
            url, item_name, checked_at, parser_result, response.status_code, raw_html,
            include_html, anti_bot_detected, "Steam rate limit HTTP 429"
        )

    if response.status_code != 200:
        parser_result.status = "ERROR"
        return result_from_parser(
            url, item_name, checked_at, parser_result, response.status_code, raw_html,
            include_html, anti_bot_detected, f"Unexpected HTTP status {response.status_code}"
        )

    if not raw_html.strip():
        return result_from_parser(
            url, item_name, checked_at, parser_result, response.status_code, raw_html,
            include_html, anti_bot_detected, "Empty HTML response"
        )

    if anti_bot_detected:
        parser_result.status = "ERROR"
        return result_from_parser(
            url, item_name, checked_at, parser_result, response.status_code, raw_html,
            include_html, True, "Possible Steam anti-bot page detected"
        )

    return result_from_parser(
        url, item_name, checked_at, parser_result, response.status_code, raw_html,
        include_html, anti_bot_detected
    )
