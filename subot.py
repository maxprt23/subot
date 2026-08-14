#!/usr/bin/env python3
import argparse
from email.header import Header
import json
import os
import random
import re
import sys
import tempfile
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SEEN_PATH = os.path.join(BASE_DIR, "seen.json")

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_seen(path):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return set(json.load(f))


def save_seen(path, ids):
    directory = os.path.dirname(os.path.abspath(path))
    basename = os.path.basename(path)
    fd, temporary_path = tempfile.mkstemp(
        dir=directory,
        prefix=f".{basename}.",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def fetch_page(url, user_agent):
    headers = {
        "User-Agent": user_agent,
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=30)
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
        "id": aid,
        "subject": item.get("subject", ""),
        "price": price,
        "url": (item.get("urls") or {}).get("default", ""),
        "town": (geo.get("town") or {}).get("value", ""),
        "city": (geo.get("city") or {}).get("value", ""),
        "date": item.get("date", ""),
    }


def matches_price(price, min_price, max_price):
    if price is None:
        return False
    if min_price is not None and price < min_price:
        return False
    if max_price is not None and price > max_price:
        return False
    return True


def fmt_price(p):
    return str(int(p)) if p is not None and p == int(p) else str(p)


def next_sleep(cfg):
    default = int(cfg.get("poll_interval", 300))
    lo = int(cfg.get("poll_interval_min", default))
    hi = int(cfg.get("poll_interval_max", default))
    if lo > hi:
        lo, hi = hi, lo
    return random.randint(lo, hi)


def encode_header(value):
    value = str(value).replace("\r", " ").replace("\n", " ")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return Header(value, "utf-8").encode()
    return value


def notify(cfg, item):
    location = " ".join(x for x in [item["town"], f"({item['city']})" if item["city"] else ""] if x)
    body = "\n".join(
        x
        for x in [
            item["subject"],
            f"{fmt_price(item['price'])} \u20ac" if item["price"] is not None else "prezzo n.d.",
            location,
            item["url"],
        ]
        if x
    )

    endpoint = f"{cfg['ntfy_server'].rstrip('/')}/{cfg['ntfy_topic']}"
    headers = {"Title": encode_header(item["subject"]), "Click": item["url"]}
    if cfg.get("ntfy_token"):
        headers["Authorization"] = f"Bearer {cfg['ntfy_token']}"

    r = requests.post(endpoint, data=body.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()


def run_once(cfg, seen, dry_run):
    html = fetch_page(cfg["search_url"], cfg.get("user_agent") or DEFAULT_UA)
    items = extract_items(html)
    print(f"fetched {len(items)} listings")
    notification_failures = 0

    for raw in items:
        it = parse_item(raw)
        if not it:
            continue
        if it["id"] in seen:
            continue
        if not matches_price(it["price"], cfg.get("min_price"), cfg.get("max_price")):
            continue

        print(f"  MATCH  {fmt_price(it['price'])} \u20ac  {it['subject']}  {it['url']}")
        if dry_run:
            continue

        try:
            notify(cfg, it)
        except requests.RequestException as e:
            print(f"  -> notification failed for {it['id']}: {e}", file=sys.stderr)
            notification_failures += 1
            continue

        seen.add(it["id"])
        print(f"  -> notified {it['id']}")
    return len(items), notification_failures


def main():
    ap = argparse.ArgumentParser(description="Subito.it watcher with ntfy notifications")
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--dry-run", action="store_true", help="do not send notifications or update seen state")
    args = ap.parse_args()

    cfg = load_config(CONFIG_PATH)
    seen = load_seen(SEEN_PATH)

    while True:
        notification_failures = 0
        try:
            _, notification_failures = run_once(cfg, seen, dry_run=args.dry_run)
            if not args.dry_run:
                save_seen(SEEN_PATH, seen)
        except requests.RequestException as e:
            print(f"error: {e}", file=sys.stderr)
            if args.once:
                return 1
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            print(f"parse error: {e}", file=sys.stderr)
            if args.once:
                return 1

        if args.once:
            return 1 if notification_failures else 0
        time.sleep(next_sleep(cfg))


if __name__ == "__main__":
    sys.exit(main())
