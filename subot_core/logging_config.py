"""Shared logging configuration for the command and its child processes."""

import logging


def configure_logging():
    """Configure the application's standard logging format."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
