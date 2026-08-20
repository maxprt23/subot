import json
import re

from curl_cffi import requests


def fetch_page(url):
    headers = {
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=30, impersonate="chrome")
    r.raise_for_status()
    return r.text


def extract_items(html):
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        raise ValueError("Subito response does not contain __NEXT_DATA__")
    data = json.loads(m.group(1))
    items = data["props"]["pageProps"]["initialState"]["items"]
    out = []
    seen = set()
    for item in items.get("originalList") or []:
        identity = item.get("urn") or (item.get("urls") or {}).get("default")
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        out.append(item)
    return out


def parse_price(features):
    p = features.get("/price")
    if not p:
        return None
    vals = p.get("values") or []
    if not vals:
        return None
    s = str(vals[0].get("key", "")).strip().replace("\u20ac", "").replace(" ", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_item(item):
    m = re.search(r":list:(\d+)", item.get("urn", ""))
    if m:
        aid = m.group(1)
    else:
        m = re.search(r"-(\d+)\.htm$", (item.get("urls") or {}).get("default", ""))
        aid = m.group(1) if m else None
    if not aid:
        return None

    geo = item.get("geo") or {}
    price = parse_price(item.get("features") or {})
    if price is not None and price <= 0:
        price = None

    return {
        "id": f"subito:{aid}",
        "subject": item.get("subject", ""),
        "price": price,
        "url": (item.get("urls") or {}).get("default", ""),
        "town": (geo.get("town") or {}).get("value", ""),
        "city": (geo.get("city") or {}).get("value", ""),
    }
