"""Fetch and parse Vinted Italy catalog pages.

Vinted has served catalog data through more than one Next.js surface.  The
catalog parser therefore works from the structured JSON in the page (including
React Server Components/Flight strings) and does not depend on rendered card
classes or category names.
"""

import json
import math
import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from curl_cffi import requests


BASE_URL = "https://www.vinted.it"
_ALLOWED_VINTED_HOSTS = {"vinted.it", "www.vinted.it"}
_ITEM_URL_RE = re.compile(r"^/items/(\d+)(?:[-/?#]|$)", re.IGNORECASE)
_SCRIPT_RE = re.compile(
    r"<script\b[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL
)
_NEXT_FLIGHT_RE = re.compile(r"self\.__next_f\.push\s*\(", re.IGNORECASE)
_JSON_DECODER = json.JSONDecoder()
_ID_KEYS = (
    "id",
    "item_id",
    "itemId",
    "marketplace_item_id",
    "marketplaceItemId",
)
_TITLE_KEYS = ("title", "subject", "name")
_URL_KEYS = (
    "url",
    "item_url",
    "itemUrl",
    "web_url",
    "webUrl",
    "href",
    "path",
)
_PRICE_KEYS = (
    "price",
    "price_amount",
    "priceAmount",
    "total_item_price",
    "totalItemPrice",
)
_CHALLENGE_MARKERS = (
    "just a moment",
    "enable javascript and cookies",
    "checking your browser",
    "attention required! | cloudflare",
    "challenge-platform",
    "cf-chl-",
    "verify you are human",
    "access denied",
)


def fetch_page(url):
    """Fetch one catalog page using the same HTTP contract as the Subito parser."""

    headers = {
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }
    response = requests.get(
        url,
        headers=headers,
        timeout=30,
        impersonate="chrome",
    )
    response.raise_for_status()
    return response.text


def _balanced_json_text(text, start):
    """Return a balanced JSON-ish array/object beginning at ``start``."""

    opening = text[start]
    if opening not in "[{":
        return None
    closing = {"[": "]", "{": "}"}
    stack = [opening]
    in_string = False
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack or char != closing[stack[-1]]:
                return None
            stack.pop()
            if not stack:
                return text[start : index + 1]
    return None


def _next_flight_values(script):
    """Decode values passed to Next's ``self.__next_f.push`` calls."""

    values = []
    for match in _NEXT_FLIGHT_RE.finditer(script):
        start = match.end()
        while start < len(script) and script[start].isspace():
            start += 1
        if start >= len(script):
            continue
        payload_text = _balanced_json_text(script, start)
        if payload_text is None:
            continue
        try:
            payload = json.loads(payload_text)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, list):
            values.extend(value for value in payload if isinstance(value, str))
        elif isinstance(payload, str):
            values.append(payload)
    return values


def _named_mapping_values(text, key):
    """Yield mapping values belonging to a quoted key in structured data."""

    pattern = re.compile(rf"([\"']){re.escape(key)}\1\s*:")
    for match in pattern.finditer(text):
        start = match.end()
        while start < len(text) and text[start].isspace():
            start += 1
        try:
            value, _ = _JSON_DECODER.raw_decode(text, start)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            yield value


def _script_values(script):
    """Yield possible structured roots from one HTML script body."""

    try:
        root = json.loads(script)
    except (TypeError, json.JSONDecodeError):
        root = None
    if isinstance(root, (dict, list)):
        yield root
        return

    flight_values = _next_flight_values(script)
    for text in (script, *flight_values):
        yield from _named_mapping_values(text, "initialCatalogState")


def _text_value(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _raw_url(item):
    for key in _URL_KEYS:
        value = item.get(key)
        if isinstance(value, Mapping):
            for nested_key in ("url", "href", "path"):
                nested = _text_value(value.get(nested_key))
                if nested:
                    return nested
        else:
            text = _text_value(value)
            if text:
                return text
    return None


def _explicit_native_id(item):
    for key in _ID_KEYS:
        value = _text_value(item.get(key))
        if value:
            return value
    return None


def _item_url_id(raw_url):
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    raw_url = raw_url.strip()
    try:
        if raw_url.startswith("//"):
            parsed = urlsplit("https:" + raw_url)
        else:
            parsed = urlsplit(raw_url)
    except ValueError:
        return None
    if parsed.scheme:
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            return None
        if (parsed.hostname or "").lower() not in _ALLOWED_VINTED_HOSTS:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        default_port = 443 if scheme == "https" else 80
        if port not in {None, default_port}:
            return None
    elif parsed.netloc:
        if (parsed.hostname or "").lower() not in _ALLOWED_VINTED_HOSTS:
            return None
    path = parsed.path or raw_url
    if not path.startswith("/"):
        path = "/" + path
    match = _ITEM_URL_RE.match(path)
    return match.group(1) if match else None


def _native_id(item):
    explicit = _explicit_native_id(item)
    if explicit:
        return explicit
    return _item_url_id(_raw_url(item))


def _title(item):
    for key in _TITLE_KEYS:
        value = item.get(key)
        if isinstance(value, Mapping):
            value = value.get("text") or value.get("value")
        value = _text_value(value)
        if value:
            return value
    return ""


def _price_candidate(item):
    for key in _PRICE_KEYS:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, Mapping):
            for nested_key in ("amount", "value", "raw", "price"):
                nested = value.get(nested_key)
                if nested is not None:
                    return nested
        else:
            return value
    return None


def _is_listing_record(value):
    if not isinstance(value, Mapping):
        return False
    raw_url = _raw_url(value)
    url_identity = _item_url_id(raw_url)
    identity = _explicit_native_id(value) or url_identity
    if not identity or not url_identity or identity != url_identity:
        return False
    return bool(_title(value))


def _catalog_records(value):
    """Return records only from a recognized Vinted catalog-state shape."""

    if not isinstance(value, Mapping):
        return [], False

    catalog_state = value.get("initialCatalogState", value)
    if not isinstance(catalog_state, Mapping):
        return [], False

    items_state = catalog_state.get("items")
    if isinstance(items_state, Mapping):
        items = items_state.get("items")
        pagination = items_state.get("pagination")
    else:
        items = items_state
        pagination = catalog_state.get("pagination")

    if not isinstance(items, (list, tuple)) or not isinstance(
        pagination, Mapping
    ):
        return [], False

    records = [item for item in items if _is_listing_record(item)]
    return records, not items


def _looks_like_challenge(html):
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def extract_items(html):
    """Extract listings and turn excessive nesting into a parse error."""

    try:
        return _extract_items(html)
    except RecursionError as error:
        raise ValueError("Vinted response is too deeply nested") from error


def _extract_items(html):
    """Extract unique raw catalog listing dictionaries from a Vinted page.

    Both ordinary embedded JSON and Next App Router Flight payloads are
    inspected.  Item identity is the native Vinted ID, not the result-card
    position or category metadata.
    """

    if not isinstance(html, str) or not html.strip():
        raise ValueError("Vinted response does not contain structured listing data")

    source_texts = [body for body in _SCRIPT_RE.findall(html) if body]
    if not source_texts:
        source_texts = [html]

    records = []
    has_verified_empty_catalog = False
    for source in source_texts:
        roots = list(_script_values(source))
        for root in roots:
            (
                found,
                verified_empty_catalog,
            ) = _catalog_records(root)
            records.extend(found)
            has_verified_empty_catalog = (
                has_verified_empty_catalog or verified_empty_catalog
            )

    unique = []
    seen = set()
    for item in records:
        identity = _native_id(item)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        unique.append(item)

    if unique:
        return unique
    if has_verified_empty_catalog:
        return []
    if _looks_like_challenge(html):
        raise ValueError("Vinted response is an anti-bot challenge page")
    raise ValueError("Vinted response does not contain structured listing data")


def _coerce_price(value):
    if isinstance(value, Mapping):
        for key in ("amount", "value", "raw", "price"):
            if key in value:
                return _coerce_price(value[key])
        return None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if not isinstance(value, str):
        return None

    text = value.replace("\xa0", " ").strip()
    text = re.sub(r"[^0-9,\.\-+]", "", text)
    if not text or text in {"+", "-", ".", ","}:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        if text.count(",") == 1:
            integer, fractional = text.split(",")
            if integer.lstrip("+-").isdigit() and len(fractional) == 3:
                text = integer + fractional
            else:
                text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "." in text:
        if text.count(".") == 1:
            integer, fractional = text.split(".")
            if integer.lstrip("+-").isdigit() and len(fractional) == 3:
                text = integer + fractional
        else:
            text = text.replace(".", "")
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _canonical_url(raw_url, identity):
    url_identity = _item_url_id(raw_url)
    if not raw_url or not url_identity or url_identity != identity:
        return None
    parsed = urlsplit(raw_url)
    if not parsed.scheme:
        if raw_url.startswith("//"):
            parsed = urlsplit("https:" + raw_url)
        else:
            parsed = urlsplit(
                BASE_URL
                + (raw_url if raw_url.startswith("/") else "/" + raw_url)
            )
    return urlunsplit(
        (
            "https",
            "www.vinted.it",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def parse_item(item):
    """Normalize one raw Vinted listing to the runner's common payload shape."""

    if not isinstance(item, Mapping):
        return None
    identity = _native_id(item)
    raw_url = _raw_url(item)
    if not identity or _item_url_id(raw_url) != identity:
        return None

    price = _coerce_price(_price_candidate(item))
    if price is not None and price <= 0:
        price = None

    return {
        "id": f"vinted:{identity}",
        "subject": _title(item),
        "price": price,
        "url": _canonical_url(raw_url, identity),
        "town": "",
        "city": "",
    }
