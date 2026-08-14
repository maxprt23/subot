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
    search_urls = get_search_urls(cfg)
    LOGGER.info(
        "startup searches=%d search_origins=%s ntfy_origin=%s poll_interval_min=%s "
        "poll_interval_max=%s dry_run=%s once=%s",
        len(search_urls),
        ",".join(sorted(set(url_origin(url) for url in search_urls))),
        url_origin(cfg.get("ntfy_server", "")),
        cfg.get("poll_interval_min"),
        cfg.get("poll_interval_max"),
        dry_run,
        once,
    )


def get_search_urls(cfg):
    urls = cfg.get("search_urls")
    if not isinstance(urls, list) or not urls:
        raise ValueError("search_urls must be a non-empty list")
    if any(not isinstance(url, str) or not url.strip() for url in urls):
        raise ValueError("search_urls entries must be non-empty strings")
    return urls


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


def poll_interval_bounds(cfg):
    lo = int(cfg["poll_interval_min"])
    hi = int(cfg["poll_interval_max"])
    if lo > hi:
        raise ValueError("poll_interval_min must not exceed poll_interval_max")
    if lo < 1:
        raise ValueError("poll intervals must be positive")
    return lo, hi


def next_sleep(cfg):
    lo, hi = poll_interval_bounds(cfg)
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


def run_once(cfg, seen, dry_run, stats, search_url=None):
    # Keep the optional lookup for callers that check one search directly.
    if search_url is None:
        search_url = cfg["search_url"]
    html = fetch_page(search_url)
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


def run_search(cfg, search_url, search_number, search_count, seen, dry_run):
    stats = CycleStats()
    try:
        run_once(
            cfg,
            seen,
            dry_run=dry_run,
            stats=stats,
            search_url=search_url,
        )
    except RequestException as e:
        stats.failures += 1
        LOGGER.error(
            "fetch failed search=%d/%d origin=%s error_type=%s",
            search_number,
            search_count,
            url_origin(search_url),
            type(e).__name__,
        )
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        stats.failures += 1
        LOGGER.error(
            "response parsing failed search=%d/%d origin=%s error_type=%s",
            search_number,
            search_count,
            url_origin(search_url),
            type(e).__name__,
        )

    return stats


def log_summary(stats, search_number, search_count, search_url, next_poll_seconds):
    LOGGER.log(
        logging.WARNING if stats.failures else logging.INFO,
        "polling cycle summary search=%d/%d origin=%s fetched=%d matched=%d "
        "notified=%d failures=%d next_poll_seconds=%s",
        search_number,
        search_count,
        url_origin(search_url),
        stats.fetched,
        stats.matched,
        stats.notified,
        stats.failures,
        next_poll_seconds if next_poll_seconds is not None else "none",
    )


def run_all_once(cfg, search_urls, seen, dry_run, seen_path):
    failures = 0
    search_count = len(search_urls)
    for index, search_url in enumerate(search_urls, start=1):
        stats = run_search(
            cfg, search_url, index, search_count, seen, dry_run
        )

        if not dry_run:
            try:
                save_seen(seen_path, seen)
            except OSError as e:
                stats.failures += 1
                LOGGER.error(
                    "state persistence failed search=%d/%d error_type=%s",
                    index,
                    search_count,
                    type(e).__name__,
                )

        failures += stats.failures
        log_summary(stats, index, search_count, search_url, None)

    return 1 if failures else 0


def make_schedule(search_urls, now):
    return {index: now for index in range(len(search_urls))}


def run_continuously(cfg, search_urls, seen, dry_run, seen_path):
    poll_interval_bounds(cfg)
    search_count = len(search_urls)
    deadlines = make_schedule(search_urls, time.monotonic())

    while True:
        search_index = min(deadlines, key=deadlines.get)
        delay = deadlines[search_index] - time.monotonic()
        if delay > 0:
            time.sleep(delay)

        search_url = search_urls[search_index]
        stats = run_search(
            cfg,
            search_url,
            search_index + 1,
            search_count,
            seen,
            dry_run,
        )

        # Always move this search into the future, including after a failure.
        sleep_seconds = next_sleep(cfg)
        deadlines[search_index] = time.monotonic() + sleep_seconds

        if not dry_run:
            try:
                save_seen(seen_path, seen)
            except OSError as e:
                stats.failures += 1
                LOGGER.error(
                    "state persistence failed search=%d/%d error_type=%s",
                    search_index + 1,
                    search_count,
                    type(e).__name__,
                )

        log_summary(
            stats,
            search_index + 1,
            search_count,
            search_url,
            sleep_seconds,
        )


def main():
    configure_logging()
    ap = argparse.ArgumentParser(description="Subito.it watcher with ntfy notifications")
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--dry-run", action="store_true", help="do not send notifications or update seen state")
    args = ap.parse_args()

    cfg = load_config(CONFIG_PATH)
    seen = load_seen(SEEN_PATH)
    search_urls = get_search_urls(cfg)
    log_startup_config(cfg, dry_run=args.dry_run, once=args.once)

    if args.once:
        return run_all_once(
            cfg, search_urls, seen, args.dry_run, SEEN_PATH
        )
    run_continuously(cfg, search_urls, seen, args.dry_run, SEEN_PATH)


if __name__ == "__main__":
    sys.exit(main())
