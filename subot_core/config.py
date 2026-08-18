import json
import random
from urllib.parse import urlsplit


OPENROUTER_STRING_SETTINGS = (
    "openrouter_api_key",
    "openrouter_model",
    "llm_system_prompt",
    "llm_rules",
)
OPENROUTER_INTEGER_SETTINGS = (
    "llm_max_retries",
    "llm_web_search_max_results",
    "llm_web_search_max_total_results",
    "llm_web_fetch_max_uses",
    "llm_web_fetch_max_content_tokens",
)
REASONING_EFFORTS = frozenset(
    ("none", "minimal", "low", "medium", "high", "xhigh", "max")
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


def llm_enabled(cfg):
    """Return whether listings require an LLM decision before notification."""

    value = cfg.get("use_llm", True)
    if not isinstance(value, bool):
        raise ValueError("use_llm must be a boolean")
    return value


def get_retry_limit(cfg, default=None):
    """Return a validated processing retry limit."""

    value = cfg.get("llm_max_retries", default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("llm_max_retries must be an integer")
    if value < 0:
        raise ValueError("llm_max_retries must not be negative")
    if value > 3:
        raise ValueError("llm_max_retries must not exceed 3")
    return value


def get_openrouter_settings(cfg):
    """Return validated OpenRouter and LLM decision settings."""

    settings = {}
    for name in OPENROUTER_STRING_SETTINGS:
        value = cfg.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        settings[name] = value

    for name in OPENROUTER_INTEGER_SETTINGS:
        if name == "llm_max_retries":
            settings[name] = get_retry_limit(cfg)
            continue
        value = cfg.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if value < 1:
            raise ValueError(f"{name} must be positive")
        settings[name] = value

    if settings["llm_web_search_max_results"] > 25:
        raise ValueError("llm_web_search_max_results must not exceed 25")
    if (
        settings["llm_web_search_max_total_results"]
        < settings["llm_web_search_max_results"]
    ):
        raise ValueError(
            "llm_web_search_max_total_results must be at least "
            "llm_web_search_max_results"
        )

    reasoning_effort = cfg.get("llm_reasoning_effort")
    if reasoning_effort is not None:
        if not isinstance(reasoning_effort, str) or reasoning_effort not in REASONING_EFFORTS:
            allowed = ", ".join(sorted(REASONING_EFFORTS))
            raise ValueError(
                "llm_reasoning_effort must be null or one of: " f"{allowed}"
            )
    settings["llm_reasoning_effort"] = reasoning_effort
    return settings


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
