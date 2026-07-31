"""Structured logging configuration.

Provides a ``setup_logging`` helper that configures Python's standard
logging module with a JSON-compatible formatter and appropriate handlers.
"""

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging with a structured format.

    Args:
        level: Logging level string (e.g. "DEBUG", "INFO", "WARNING").
    """
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )
