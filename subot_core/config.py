import json
import random
from urllib.parse import urlsplit


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
