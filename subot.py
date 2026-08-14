#!/usr/bin/env python3
import argparse
from dataclasses import dataclass
from email.header import Header
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SEEN_PATH = os.path.join(BASE_DIR, "seen.json")
LOGGER = logging.getLogger("subot")


@dataclass
class CycleStats:
    fetched: int = 0
    matched: int = 0
    notified: int = 0
    failures: int = 0


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def url_origin(value):
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "invalid"
    if not parsed.scheme or not parsed.hostname:
        return "invalid"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return "invalid"
    return f"{parsed.scheme}://{host}{port}"


def log_startup_config(cfg, dry_run, once):
    LOGGER.info(
        "startup search_origin=%s ntfy_origin=%s poll_interval_min=%s "
        "poll_interval_max=%s dry_run=%s once=%s",
        url_origin(cfg.get("search_url", "")),
        url_origin(cfg.get("ntfy_server", "")),
        cfg.get("poll_interval_min"),
        cfg.get("poll_interval_max"),
        dry_run,
        once,
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
        "id": aid,
        "subject": item.get("subject", ""),
        "price": price,
        "url": (item.get("urls") or {}).get("default", ""),
        "town": (geo.get("town") or {}).get("value", ""),
        "city": (geo.get("city") or {}).get("value", ""),
    }


def fmt_price(p):
    return str(int(p)) if p == int(p) else str(p)


def next_sleep(cfg):
    lo = int(cfg["poll_interval_min"])
    hi = int(cfg["poll_interval_max"])
    if lo > hi:
        raise ValueError("poll_interval_min must not exceed poll_interval_max")
    if lo < 1:
        raise ValueError("poll intervals must be positive")
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
            f"{fmt_price(item['price'])} \u20ac",
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


def run_once(cfg, seen, dry_run, stats):
    html = fetch_page(cfg["search_url"])
    items = extract_items(html)
    stats.fetched = len(items)

    for raw in items:
        it = parse_item(raw)
        if not it:
            continue
        if it["id"] in seen:
            continue
        if it["price"] is None:
            continue

        stats.matched += 1
        LOGGER.info(
            "listing matched id=%s price=%s subject=%r url=%s",
            it["id"],
            fmt_price(it["price"]),
            it["subject"],
            it["url"],
        )
        if dry_run:
            continue

        try:
            notify(cfg, it)
        except (RequestException, KeyError) as e:
            LOGGER.error(
                "notification failed id=%s error_type=%s",
                it["id"],
                type(e).__name__,
            )
            stats.failures += 1
            continue

        seen.add(it["id"])
        stats.notified += 1
        LOGGER.info("notification delivered id=%s", it["id"])


def main():
    configure_logging()
    ap = argparse.ArgumentParser(description="Subito.it watcher with ntfy notifications")
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--dry-run", action="store_true", help="do not send notifications or update seen state")
    args = ap.parse_args()

    cfg = load_config(CONFIG_PATH)
    seen = load_seen(SEEN_PATH)
    log_startup_config(cfg, dry_run=args.dry_run, once=args.once)

    while True:
        stats = CycleStats()
        sleep_seconds = None if args.once else next_sleep(cfg)
        try:
            run_once(cfg, seen, dry_run=args.dry_run, stats=stats)
            if not args.dry_run:
                save_seen(SEEN_PATH, seen)
        except RequestException as e:
            stats.failures += 1
            LOGGER.error("fetch failed error_type=%s", type(e).__name__)
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            stats.failures += 1
            LOGGER.error("response parsing failed error_type=%s", type(e).__name__)
        except OSError as e:
            stats.failures += 1
            LOGGER.error("state persistence failed error_type=%s", type(e).__name__)

        LOGGER.log(
            logging.WARNING if stats.failures else logging.INFO,
            "polling cycle summary fetched=%d matched=%d notified=%d failures=%d "
            "next_poll_seconds=%s",
            stats.fetched,
            stats.matched,
            stats.notified,
            stats.failures,
            sleep_seconds if sleep_seconds is not None else "none",
        )

        if args.once:
            return 1 if stats.failures else 0
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    sys.exit(main())
