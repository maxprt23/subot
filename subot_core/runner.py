"""Polling orchestration for the Subito watcher.

The functions in this module contain the application's polling loop but keep
configuration, persistence, fetching, and notification details in their own
modules.  The logger name is intentionally kept as ``subot`` for compatibility
with the original single-file application and its service configuration.
"""

import logging
import time
from contextlib import contextmanager

import fcntl

from curl_cffi.requests.exceptions import RequestException

from .config import (
    get_openrouter_settings,
    next_sleep,
    poll_interval_bounds,
    url_origin,
)
from .models import CycleStats
from .ntfy import fmt_price, notify
from .openrouter import OpenRouterClient
from .state import StateStore, save_seen, search_key
from .subito import extract_items, fetch_page, parse_item


LOGGER = logging.getLogger("subot")
WORKER_IDLE_SECONDS = 0.25


@contextmanager
def _worker_state_lock(state_path):
    """Hold the exclusive worker lock for the lifetime of a worker.

    The lock is placed on the SQLite state path itself so a second supervisor
    using the same state database cannot recover and process claims while the
    original worker is still alive.  ``flock`` is released automatically when
    the descriptor closes after a normal return or an abnormal process exit.
    """

    with open(state_path, "a+") as lock_file:
        try:
            fcntl.flock(
                lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except (BlockingIOError, OSError) as error:
            LOGGER.error(
                "worker lock unavailable state_path=%s",
                state_path,
            )
            raise SystemExit(1) from error

        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def poll_search_once(cfg, search_url, store, dry_run, stats):
    """Fetch one search and baseline or enqueue listings without waiting.

    The poller never calls the LLM or ntfy.  Outside a first-poll baseline,
    it only stores each globally new, positively priced listing as durable
    work for the independent worker process.
    """

    html = fetch_page(search_url)
    raw_items = extract_items(html)
    stats.fetched = len(raw_items)
    items = [item for raw in raw_items if (item := parse_item(raw))]

    key = search_key(search_url)
    if not store.is_search_initialized(key):
        if dry_run:
            stats.matched = sum(
                item["price"] is not None
                and not store.is_listing_known(item["id"])
                for item in items
            )
            return
        inserted = store.initialize_search(key, items)
        if inserted is not None:
            stats.baselined = inserted
            LOGGER.info(
                "search baseline initialized origin=%s listings=%d",
                url_origin(search_url),
                inserted,
            )
            return

    for item in items:
        if item["price"] is None:
            continue
        if dry_run:
            if store.is_listing_known(item["id"]):
                continue
            stats.matched += 1
            continue
        if not store.enqueue_listing(item):
            continue
        stats.matched += 1
        LOGGER.info(
            "listing queued id=%s price=%s subject=%r url=%s",
            item["id"],
            fmt_price(item["price"]),
            item["subject"],
            item["url"],
        )


def run_queued_search(cfg, search_url, search_number, search_count, store, dry_run):
    """Run a queue-backed poll and turn fetch/parse errors into stats."""

    stats = CycleStats()
    try:
        poll_search_once(cfg, search_url, store, dry_run, stats)
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


def run_poller_process(stop_event, poller_done_event, once, cfg, search_urls, state_path, dry_run):
    """Child-process target that polls independently from LLM work."""

    del poller_done_event
    with StateStore(state_path) as store:
        search_count = len(search_urls)
        if once:
            failures = 0
            for index, search_url in enumerate(search_urls, start=1):
                if stop_event.is_set():
                    break
                stats = run_queued_search(
                    cfg, search_url, index, search_count, store, dry_run
                )
                failures += stats.failures
                log_summary(stats, index, search_count, search_url, None)
            if failures:
                raise SystemExit(1)
            return

        poll_interval_bounds(cfg)
        now = time.monotonic()
        deadlines = {index: now for index in range(search_count)}
        while not stop_event.is_set():
            search_index = min(deadlines, key=deadlines.get)
            delay = deadlines[search_index] - time.monotonic()
            if delay > 0:
                stop_event.wait(delay)
                if stop_event.is_set():
                    break

            search_url = search_urls[search_index]
            stats = run_queued_search(
                cfg,
                search_url,
                search_index + 1,
                search_count,
                store,
                dry_run,
            )
            sleep_seconds = next_sleep(cfg)
            deadlines[search_index] = time.monotonic() + sleep_seconds
            log_summary(
                stats,
                search_index + 1,
                search_count,
                search_url,
                sleep_seconds,
            )


def openrouter_client_from_config(cfg):
    """Build the worker's OpenRouter client from validated configuration."""

    settings = get_openrouter_settings(cfg)
    return OpenRouterClient(
        api_key=settings["openrouter_api_key"],
        model_id=settings["openrouter_model"],
        system_prompt=settings["llm_system_prompt"],
        rules=settings["llm_rules"],
        web_search_max_results=settings["llm_web_search_max_results"],
        web_search_max_total_results=settings[
            "llm_web_search_max_total_results"
        ],
        web_fetch_max_uses=settings["llm_web_fetch_max_uses"],
        web_fetch_max_content_tokens=settings[
            "llm_web_fetch_max_content_tokens"
        ],
    )


def process_claimed_job(cfg, store, client, job):
    """Decide and deliver one claimed listing, preserving retryable failures."""

    try:
        should_notify = client.decide(job.payload)
        if should_notify:
            notify(cfg, job.payload)
            store.mark_notified(job.listing_id)
            LOGGER.info("listing notified id=%s", job.listing_id)
        else:
            store.mark_rejected(job.listing_id)
            LOGGER.info("listing rejected id=%s", job.listing_id)
        return True
    except Exception as error:
        error_type = type(error).__name__
        store.fail_or_retry(
            job.listing_id,
            error_type,
            max_retries=cfg["llm_max_retries"],
        )
        LOGGER.error(
            "listing processing failed id=%s error_type=%s",
            job.listing_id,
            error_type,
        )
        return False


def run_worker_process(stop_event, poller_done_event, once, cfg, state_path, dry_run):
    """Child-process target that drains queued listings through OpenRouter."""

    if dry_run:
        return
    with _worker_state_lock(state_path):
        with StateStore(state_path) as store:
            store.recover_claimed_jobs()
            client = openrouter_client_from_config(cfg)
            while not stop_event.is_set():
                job = store.claim_next()
                if job is None:
                    if once and poller_done_event.is_set() and store.all_terminal():
                        return
                    stop_event.wait(WORKER_IDLE_SECONDS)
                    continue
                process_claimed_job(cfg, store, client, job)


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
