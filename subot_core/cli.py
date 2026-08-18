"""Command-line entry point for the Subito watcher."""

import argparse
import logging
import os
import time

from .config import (
    get_openrouter_settings,
    get_retry_limit,
    get_search_urls,
    llm_enabled,
    load_config,
    next_sleep,
    poll_interval_bounds,
    url_origin,
)
from .logging_config import configure_logging
from .runner import (
    log_summary,
    run_poller_process,
    run_queued_search,
    run_worker_process,
)
from .state import StateStore
from .supervisor import run_supervisor


# Resolve paths relative to the project root, not the process working directory.
PACKAGE_DIR = os.path.dirname(os.path.realpath(__file__))
BASE_DIR = os.path.dirname(PACKAGE_DIR)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.sqlite3")
LOGGER = logging.getLogger("subot")


def log_startup_config(cfg, dry_run, once):
    """Log safe startup details without query strings or credentials."""

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


def queue_failure_boundary(state_path):
    """Capture the pre-run failed-listing count without creating state."""

    if not os.path.exists(state_path):
        return 0
    with StateStore(state_path, read_only=True) as store:
        return store.failure_boundary()


def queue_is_successful(state_path, failure_boundary=0):
    """Return whether once-mode queue work ended without new failures."""

    with StateStore(state_path, read_only=True) as store:
        return store.once_successful(failure_boundary)


def run_dry_run_once(search_urls, store):
    """Poll every search once through the read-only queue path."""

    failures = 0
    search_count = len(search_urls)
    for index, search_url in enumerate(search_urls, start=1):
        stats = run_queued_search(
            search_url,
            index,
            search_count,
            store,
            True,
        )
        failures += stats.failures
        log_summary(stats, index, search_count, search_url, None)
    return 1 if failures else 0


def run_dry_run_continuously(cfg, search_urls, store):
    """Continuously poll searches through a read-only queue path."""

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
        stats = run_queued_search(
            search_url,
            search_index + 1,
            search_count,
            store,
            True,
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


def open_dry_run_state(state_path):
    """Open existing SQLite state read-only, or use an ephemeral store."""

    if os.path.exists(state_path):
        return StateStore(state_path, read_only=True)
    return StateStore(":memory:")


def initialize_state(state_path):
    """Initialize SQLite before the poller and worker open it concurrently."""

    with StateStore(state_path):
        pass


def main():
    """Parse command-line options, load state, and start polling."""

    configure_logging()
    parser = argparse.ArgumentParser(
        description="Subito.it watcher with ntfy notifications"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single check and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do not send notifications or update persistent state",
    )
    args = parser.parse_args()

    cfg = load_config(CONFIG_PATH)
    search_urls = get_search_urls(cfg)
    log_startup_config(cfg, dry_run=args.dry_run, once=args.once)

    if args.dry_run:
        with open_dry_run_state(STATE_PATH) as store:
            if args.once:
                return run_dry_run_once(search_urls, store)
            return run_dry_run_continuously(cfg, search_urls, store)

    # Validate in the parent before child processes are created.  This keeps
    # configuration errors deterministic and avoids logging sensitive values.
    if llm_enabled(cfg):
        get_openrouter_settings(cfg)
    else:
        get_retry_limit(cfg, default=3)
    initialize_state(STATE_PATH)
    once_failure_boundary = queue_failure_boundary(STATE_PATH) if args.once else 0
    return run_supervisor(
        run_poller_process,
        run_worker_process,
        once=args.once,
        worker_done=(
            lambda: queue_is_successful(STATE_PATH, once_failure_boundary)
        ) if args.once else None,
        poller_args=(cfg, search_urls, STATE_PATH),
        worker_args=(cfg, STATE_PATH),
    )
