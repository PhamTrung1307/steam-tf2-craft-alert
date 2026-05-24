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
    parser_results: List[Dict[str, Any]] = field(default_factory=list)
    trusted_negative_sources: List[str] = field(default_factory=list)
    parser_conflict_detected: bool = False
    dom_visible_text_has_not_usable: bool = False
    raw_html_has_not_usable: bool = False
    final_decision_reason: str = ""


@dataclass
class SourceSignal:
    source: str
    item_name: Optional[str] = None
    name_match: bool = False
    appid: Optional[int] = None
    parsed_ok: bool = False
    detected_not_usable: bool = False
    confidence: str = "none"
    extracted_texts: List[str] = field(default_factory=list)
    matched_patterns: List[str] = field(default_factory=list)
    parsed_json: Optional[Dict[str, Any]] = None
    trusted: bool = True
    supports_craftable: bool = False
    error: Optional[str] = None

    def to_debug_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "item_name": self.item_name,
            "name_match": self.name_match,
            "appid": self.appid,
            "parsed_ok": self.parsed_ok,
            "detected_not_usable": self.detected_not_usable,
            "confidence": self.confidence,
            "extracted_texts": self.extracted_texts,
            "matched_patterns": self.matched_patterns,
            "trusted": self.trusted,
            "supports_craftable": self.supports_craftable,
            "error": self.error,
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
    matched_patterns: List[str] = field(default_factory=list)
    description_html: Optional[str] = None
    description_text: Optional[str] = None
    extracted_texts: List[str] = field(default_factory=list)
    parsed_json: Optional[Dict[str, Any]] = None
    parsed_item_name: Optional[str] = None
    appid: Optional[int] = None
    parse_method: str = "parser_failed"
    used_layout: str = "unknown"
    parser_results: List[Dict[str, Any]] = field(default_factory=list)
    trusted_negative_sources: List[str] = field(default_factory=list)
    parser_conflict_detected: bool = False
    dom_visible_text_has_not_usable: bool = False
    raw_html_has_not_usable: bool = False
    final_decision_reason: str = ""
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


def normalize_raw_html_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = decode_backslash_escapes(text)
    text = html.unescape(text).lower()
    text = re.sub(r"<[^>]+>", " ", text)
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


def extract_dom_visible_texts(raw_html: str) -> List[str]:
    soup = BeautifulSoup(raw_html, "html.parser")
    selectors = [
        ".market_listing_item_descriptors",
        "#largeiteminfo",
        "#largeiteminfo_item_descriptors",
        "#largeiteminfo_react_placeholder",
        ".market_listing_iteminfo",
        "#market_listing_iteminfo",
        ".market_commodity_order_block",
        "#market_commodity_order_block",
        ".descriptor",
    ]
    texts: List[str] = []
    for selector in selectors:
        for element in soup.select(selector):
            text = normalize_text(element.get_text(" ", strip=True))
            if text and text not in texts:
                texts.append(text)
    return texts


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


def build_structured_signal(
    source: str,
    candidates: List[Dict[str, Any]],
    expected_item_name: str,
    parsed_ok: bool,
    parsed_json: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> SourceSignal:
    if not candidates:
        return SourceSignal(source=source, parsed_ok=False, parsed_json=parsed_json, error=error)

    extracted_texts: List[str] = []
    matched_patterns: List[str] = []
    detected_not_usable = False
    supports_craftable = False
    first = candidates[0]

    for candidate in candidates:
        texts = collect_item_texts(candidate)
        description_texts = collect_description_texts(candidate)
        extracted_texts.extend(text for text in texts if text and text not in extracted_texts)
        found, matches = find_not_craftable_in_texts(texts)
        if found:
            detected_not_usable = True
            matched_patterns.extend(match for match in matches if match not in matched_patterns)
        if (
            item_appid(candidate) == 440
            and item_name_matches(candidate, expected_item_name)
            and description_texts
        ):
            supports_craftable = True

    parsed_item_name = first.get("market_hash_name") or first.get("market_name") or first.get("name")
    appid = item_appid(first)
    name_match = any(item_name_matches(candidate, expected_item_name) for candidate in candidates)
    return SourceSignal(
        source=source,
        item_name=str(parsed_item_name) if parsed_item_name else None,
        name_match=name_match,
        appid=appid,
        parsed_ok=parsed_ok and name_match,
        detected_not_usable=detected_not_usable,
        confidence="high" if name_match and appid == 440 else "medium",
        extracted_texts=extracted_texts,
        matched_patterns=matched_patterns,
        parsed_json=parsed_json,
        trusted=True,
        supports_craftable=supports_craftable and not detected_not_usable,
        error=error,
    )


def beta_render_context_signal(raw_html: str, expected_item_name: str) -> SourceSignal:
    render_context = parse_render_context(raw_html)
    if render_context is None:
        return SourceSignal(source="beta_render_context", error="No renderContext")

    query_data = coerce_query_data(render_context)
    candidates: List[Dict[str, Any]] = []
    for candidate in iter_dicts(query_data):
        if (
            item_appid(candidate) == 440
            and item_name_matches(candidate, expected_item_name)
            and any(key in candidate for key in ("descriptions", "owner_descriptions", "fraudwarnings"))
        ):
            candidates.append(candidate)

    for candidate in iter_dicts(query_data):
        description = candidate.get("description")
        if isinstance(description, dict) and item_name_matches(description, expected_item_name):
            enriched = dict(description)
            if "appid" not in enriched and item_appid(candidate) is not None:
                enriched["appid"] = item_appid(candidate)
            if enriched not in candidates:
                candidates.append(enriched)

    return build_structured_signal(
        source="beta_render_context",
        candidates=candidates,
        expected_item_name=expected_item_name,
        parsed_ok=bool(candidates),
        parsed_json=render_context,
        error=None if candidates else "No matching TF2 item description found",
    )


def parse_beta_render_context(raw_html: str, expected_item_name: str) -> ParserResult:
    signal = beta_render_context_signal(raw_html, expected_item_name)
    return parser_result_from_signal(signal, "beta:renderContext", "beta")


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


def classic_g_rgassets_signal(raw_html: str, expected_item_name: str) -> SourceSignal:
    assets, listing_info = get_classic_assets(raw_html)
    if assets is None:
        return SourceSignal(source="classic_g_rgAssets", error="No g_rgAssets")

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
    return build_structured_signal(
        source="classic_g_rgAssets",
        candidates=matching_candidates,
        expected_item_name=expected_item_name,
        parsed_ok=bool(matching_candidates),
        error=None if matching_candidates else "No matching market name in g_rgAssets",
    )


def parse_classic_g_rgAssets(raw_html: str, expected_item_name: str) -> ParserResult:
    signal = classic_g_rgassets_signal(raw_html, expected_item_name)
    return parser_result_from_signal(signal, "classic:g_rgAssets", "classic")


def find_not_craftable_in_texts(texts: Iterable[str]) -> Tuple[bool, List[str]]:
    matches: List[str] = []
    for text in texts:
        normalized = normalize_text(text)
        for pattern in NOT_CRAFTABLE_PATTERNS:
            match = pattern.search(normalized)
            if match:
                matches.append(match.group(0))
    return bool(matches), matches


def dom_visible_text_signal(raw_html: str, expected_item_name: str) -> SourceSignal:
    texts = extract_dom_visible_texts(raw_html)
    found, matches = find_not_craftable_in_texts(texts)
    combined_text = " ".join(texts)
    return SourceSignal(
        source="DOM_visible_text",
        item_name=expected_item_name if names_match(expected_item_name, expected_item_name) else None,
        name_match=normalize_text(expected_item_name) in normalize_text(combined_text) if combined_text else False,
        parsed_ok=bool(texts),
        detected_not_usable=found,
        confidence="high" if texts else "none",
        extracted_texts=texts,
        matched_patterns=matches,
        trusted=True,
        supports_craftable=False,
        error=None if texts else "No visible DOM description text found",
    )


def raw_html_fallback_signal(raw_html: str) -> SourceSignal:
    raw_text = normalize_raw_html_text(raw_html) if raw_html else ""
    matches: List[str] = []
    excerpts: List[str] = []
    for pattern in NOT_CRAFTABLE_PATTERNS:
        for match in pattern.finditer(raw_text):
            matches.append(match.group(0))
            start = max(match.start() - 160, 0)
            end = min(match.end() + 160, len(raw_text))
            excerpt = raw_text[start:end].strip()
            if excerpt and excerpt not in excerpts:
                excerpts.append(excerpt)
    found = bool(matches)
    return SourceSignal(
        source="raw_html_fallback",
        parsed_ok=bool(raw_html),
        detected_not_usable=found,
        confidence="safety" if found else "low",
        extracted_texts=excerpts,
        matched_patterns=matches,
        trusted=True,
        supports_craftable=False,
        error=None if raw_html else "Empty HTML",
    )


def parser_result_from_signal(signal: SourceSignal, parse_method: str, used_layout: str) -> ParserResult:
    status = "UNKNOWN"
    reason = signal.error or "No conclusive signal"
    if signal.detected_not_usable:
        status = "NOT_CRAFTABLE"
        reason = f"{signal.source} detected not usable"
    elif signal.supports_craftable:
        status = "CRAFTABLE"
        reason = f"{signal.source} supports craftable"

    return ParserResult(
        status=status,
        parse_method=parse_method,
        used_layout=used_layout,
        parsed_item_name=signal.item_name,
        appid=signal.appid,
        found_not_craftable=signal.detected_not_usable,
        matched_patterns=signal.matched_patterns,
        extracted_texts=signal.extracted_texts,
        parsed_json=signal.parsed_json,
        error=signal.error,
        parser_results=[signal.to_debug_dict()],
        trusted_negative_sources=[signal.source] if signal.detected_not_usable else [],
        dom_visible_text_has_not_usable=signal.source == "DOM_visible_text" and signal.detected_not_usable,
        raw_html_has_not_usable=signal.source == "raw_html_fallback" and signal.detected_not_usable,
        final_decision_reason=reason,
    )


def fallback_raw_scan(raw_html: str) -> ParserResult:
    return parser_result_from_signal(raw_html_fallback_signal(raw_html), "fallback_raw_scan", "unknown")


def detect_status(raw_html: str, expected_item_name: str) -> ParserResult:
    signals = [
        beta_render_context_signal(raw_html, expected_item_name),
        classic_g_rgassets_signal(raw_html, expected_item_name),
        dom_visible_text_signal(raw_html, expected_item_name),
        raw_html_fallback_signal(raw_html),
    ]
    trusted_negative_signals = [
        signal for signal in signals if signal.trusted and signal.detected_not_usable
    ]
    positive_parser_signals = [
        signal
        for signal in signals
        if signal.source in {"beta_render_context", "classic_g_rgAssets"}
        and signal.supports_craftable
        and signal.appid == 440
        and signal.name_match
        and signal.parsed_ok
    ]
    parser_conflict = bool(trusted_negative_signals and positive_parser_signals)
    parser_results = [signal.to_debug_dict() for signal in signals]
    extracted_texts: List[str] = []
    for signal in signals:
        for text in signal.extracted_texts:
            if text and text not in extracted_texts:
                extracted_texts.append(text)

    dom_has_not_usable = any(
        signal.source == "DOM_visible_text" and signal.detected_not_usable
        for signal in signals
    )
    raw_has_not_usable = any(
        signal.source == "raw_html_fallback" and signal.detected_not_usable
        for signal in signals
    )

    if trusted_negative_signals:
        source_names = [signal.source for signal in trusted_negative_signals]
        parse_method = "parser_conflict" if parser_conflict else source_names[0]
        reason = (
            "PARSER_CONFLICT_DOWNGRADE_TO_NOT_CRAFTABLE"
            if parser_conflict
            else f"trusted_negative_sources={','.join(source_names)}"
        )
        first_negative = trusted_negative_signals[0]
        return ParserResult(
            status="NOT_CRAFTABLE",
            parse_method=parse_method,
            used_layout=first_negative.source,
            parsed_item_name=first_negative.item_name,
            appid=first_negative.appid,
            found_not_craftable=True,
            matched_patterns=[
                match
                for signal in trusted_negative_signals
                for match in signal.matched_patterns
            ],
            extracted_texts=extracted_texts,
            parsed_json=signals[0].parsed_json,
            parser_results=parser_results,
            trusted_negative_sources=source_names,
            parser_conflict_detected=parser_conflict,
            dom_visible_text_has_not_usable=dom_has_not_usable,
            raw_html_has_not_usable=raw_has_not_usable,
            final_decision_reason=reason,
        )

    if len(positive_parser_signals) == 1 or len(positive_parser_signals) == 2:
        selected = positive_parser_signals[0]
        return ParserResult(
            status="CRAFTABLE",
            parse_method="no_negative_signals",
            used_layout=selected.source,
            parsed_item_name=selected.item_name,
            appid=selected.appid,
            extracted_texts=extracted_texts,
            parsed_json=selected.parsed_json,
            parser_results=parser_results,
            dom_visible_text_has_not_usable=dom_has_not_usable,
            raw_html_has_not_usable=raw_has_not_usable,
            final_decision_reason="positive parser with zero negative signals",
        )

    return ParserResult(
        status="UNKNOWN",
        parse_method="parser_failed",
        used_layout="unknown",
        extracted_texts=extracted_texts,
        parsed_json=signals[0].parsed_json,
        error="No positive parser signal and no trusted negative signal",
        parser_results=parser_results,
        dom_visible_text_has_not_usable=dom_has_not_usable,
        raw_html_has_not_usable=raw_has_not_usable,
        final_decision_reason="no reliable craftable or not craftable decision",
    )


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
        parser_results=parser_result.parser_results,
        trusted_negative_sources=parser_result.trusted_negative_sources,
        parser_conflict_detected=parser_result.parser_conflict_detected,
        dom_visible_text_has_not_usable=parser_result.dom_visible_text_has_not_usable,
        raw_html_has_not_usable=parser_result.raw_html_has_not_usable,
        final_decision_reason=parser_result.final_decision_reason,
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
