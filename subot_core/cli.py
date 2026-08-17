"""Command-line entry point for the Subito watcher."""

import argparse
import logging
import os

from .config import get_search_urls, load_config, url_origin
from .runner import run_all_once, run_continuously
from .state import load_seen


# Resolve paths relative to the project root, not the process working directory.
PACKAGE_DIR = os.path.dirname(os.path.realpath(__file__))
BASE_DIR = os.path.dirname(PACKAGE_DIR)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SEEN_PATH = os.path.join(BASE_DIR, "seen.json")
LOGGER = logging.getLogger("subot")


def configure_logging():
    """Configure the application's standard logging format."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


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
        help="do not send notifications or update seen state",
    )
    args = parser.parse_args()

    cfg = load_config(CONFIG_PATH)
    state = load_seen(SEEN_PATH)
    search_urls = get_search_urls(cfg)
    log_startup_config(cfg, dry_run=args.dry_run, once=args.once)

    if args.once:
        return run_all_once(
            cfg,
            search_urls,
            state,
            args.dry_run,
            SEEN_PATH,
        )

    run_continuously(cfg, search_urls, state, args.dry_run, SEEN_PATH)
