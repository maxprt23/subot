"""Polling orchestration for the Subito watcher.

The functions in this module contain the application's polling loop but keep
configuration, persistence, fetching, and notification details in their own
modules.  The logger name is intentionally kept as ``subot`` for compatibility
with the original single-file application and its service configuration.
"""

import logging
import time

from curl_cffi.requests.exceptions import RequestException

from .config import next_sleep, poll_interval_bounds, url_origin
from .models import CycleStats
from .ntfy import fmt_price, notify
from .state import save_seen, search_key
from .subito import extract_items, fetch_page, parse_item


LOGGER = logging.getLogger("subot")


def run_once(cfg, search_url, seen, dry_run, stats, initialize=False):
    """Fetch and process one configured search URL.

    During the first normal poll of a search, all current listings are added
    to the shared seen set without sending notifications.  A dry run never
    mutates state or sends notifications, including when ``initialize`` is
    true.
    """

    html = fetch_page(search_url)
    items = extract_items(html)
    stats.fetched = len(items)

    for raw in items:
        item = parse_item(raw)
        if not item:
            continue

        if initialize and not dry_run:
            if item["id"] not in seen:
                stats.baselined += 1
            seen.add(item["id"])
            continue

        if item["id"] in seen:
            continue
        if item["price"] is None:
            continue

        stats.matched += 1
        LOGGER.info(
            "listing matched id=%s price=%s subject=%r url=%s",
            item["id"],
            fmt_price(item["price"]),
            item["subject"],
            item["url"],
        )
        if dry_run:
            continue

        try:
            notify(cfg, item)
        except (RequestException, KeyError) as error:
            LOGGER.error(
                "notification failed id=%s error_type=%s",
                item["id"],
                type(error).__name__,
            )
            stats.failures += 1
            continue

        seen.add(item["id"])
        stats.notified += 1
        LOGGER.info("notification delivered id=%s", item["id"])


def run_search(
    cfg,
    search_url,
    search_number,
    search_count,
    seen,
    dry_run,
    initialize=False,
):
    """Run one search and convert expected fetch/parse errors into stats."""

    stats = CycleStats()
    try:
        run_once(
            cfg,
            search_url,
            seen,
            dry_run=dry_run,
            stats=stats,
            initialize=initialize,
        )
    except RequestException as error:
        stats.failures += 1
        LOGGER.error(
            "fetch failed search=%d/%d origin=%s error_type=%s",
            search_number,
            search_count,
            url_origin(search_url),
            type(error).__name__,
        )
    except (ValueError, KeyError) as error:
        stats.failures += 1
        LOGGER.error(
            "response parsing failed search=%d/%d origin=%s error_type=%s",
            search_number,
            search_count,
            url_origin(search_url),
            type(error).__name__,
        )

    return stats


def log_summary(stats, search_number, search_count, search_url, next_poll_seconds):
    """Log a cycle summary without exposing URL query strings or credentials."""

    LOGGER.log(
        logging.WARNING if stats.failures else logging.INFO,
        "polling cycle summary search=%d/%d origin=%s fetched=%d baselined=%d "
        "matched=%d notified=%d failures=%d next_poll_seconds=%s",
        search_number,
        search_count,
        url_origin(search_url),
        stats.fetched,
        stats.baselined,
        stats.matched,
        stats.notified,
        stats.failures,
        next_poll_seconds if next_poll_seconds is not None else "none",
    )


def run_all_once(cfg, search_urls, state, dry_run, seen_path):
    """Check each configured search once and return a process status code."""

    failures = 0
    search_count = len(search_urls)
    for index, search_url in enumerate(search_urls, start=1):
        key = search_key(search_url)
        initialize = not dry_run and key not in state.initialized_searches
        stats = run_search(
            cfg,
            search_url,
            index,
            search_count,
            state.listing_ids,
            dry_run,
            initialize=initialize,
        )

        if initialize and not stats.failures:
            state.initialized_searches.add(key)
            LOGGER.info(
                "search baseline initialized search=%d/%d listings=%d",
                index,
                search_count,
                stats.baselined,
            )

        if not dry_run:
            try:
                save_seen(seen_path, state)
            except OSError as error:
                stats.failures += 1
                LOGGER.error(
                    "state persistence failed search=%d/%d error_type=%s",
                    index,
                    search_count,
                    type(error).__name__,
                )

        failures += stats.failures
        log_summary(stats, index, search_count, search_url, None)

    return 1 if failures else 0


def run_continuously(cfg, search_urls, state, dry_run, seen_path):
    """Continuously poll searches independently according to random intervals."""

    # Validate before starting any fetch, including in dry-run mode.
    poll_interval_bounds(cfg)
    search_count = len(search_urls)
    now = time.monotonic()
    deadlines = {index: now for index in range(search_count)}

    while True:
        search_index = min(deadlines, key=deadlines.get)
        delay = deadlines[search_index] - time.monotonic()
        if delay > 0:
            time.sleep(delay)

        search_url = search_urls[search_index]
        key = search_key(search_url)
        initialize = not dry_run and key not in state.initialized_searches
        stats = run_search(
            cfg,
            search_url,
            search_index + 1,
            search_count,
            state.listing_ids,
            dry_run,
            initialize=initialize,
        )

        if initialize and not stats.failures:
            state.initialized_searches.add(key)
            LOGGER.info(
                "search baseline initialized search=%d/%d listings=%d",
                search_index + 1,
                search_count,
                stats.baselined,
            )

        # Always move this search into the future, including after a failure.
        sleep_seconds = next_sleep(cfg)
        deadlines[search_index] = time.monotonic() + sleep_seconds

        if not dry_run:
            try:
                save_seen(seen_path, state)
            except OSError as error:
                stats.failures += 1
                LOGGER.error(
                    "state persistence failed search=%d/%d error_type=%s",
                    search_index + 1,
                    search_count,
                    type(error).__name__,
                )

        log_summary(
            stats,
            search_index + 1,
            search_count,
            search_url,
            sleep_seconds,
        )
