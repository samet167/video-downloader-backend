"""
utils/logger.py — Centralized Logging
=======================================
All modules call get_logger(__name__) to obtain a configured logger.
Logs go to stdout so they appear in the terminal when running locally.

Log level is controlled by LOG_LEVEL env var (default DEBUG for local dev).
"""

import logging
import os
import sys

# Read log level from env; default to DEBUG for local development
_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()
_NUMERIC = getattr(logging, _LEVEL, logging.DEBUG)

logging.basicConfig(
    stream=sys.stdout,
    level=_NUMERIC,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)

# Quieten noisy third-party loggers
logging.getLogger("yt_dlp").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger.

    Usage:
        from utils.logger import get_logger
        log = get_logger(__name__)
    """
    return logging.getLogger(name)
