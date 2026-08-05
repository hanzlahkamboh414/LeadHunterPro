"""Connector logger — structured logging scoped to a connector.

Provides a thin wrapper around Python's standard logging that automatically
includes the connector name in the logger path.
"""

from __future__ import annotations

import logging
from typing import Any


class ConnectorLogger:
    """Structured logger scoped to a named connector.

    All log entries are emitted under the ``connectors.<name>`` logger path,
    making it easy to filter logs by connector in production.

    Args:
        name: Connector identifier (e.g. ``"texas_procurement"``).
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(f"connectors.{name}")

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an informational message."""
        self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a warning."""
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, exc: Exception | None = None, *args: Any, **kwargs: Any) -> None:
        """Log an error, optionally including exception details.

        Args:
            msg: Log message.
            exc: Optional exception to include via ``exc_info``.
            *args, **kwargs: Passed through to :meth:`logging.Logger.error`.
        """
        if exc:
            self._logger.error(f"{msg}: {exc}", *args, exc_info=True, **kwargs)
        else:
            self._logger.error(msg, *args, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a debug message."""
        self._logger.debug(msg, *args, **kwargs)
