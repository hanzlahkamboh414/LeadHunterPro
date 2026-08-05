"""Error handler for connector operations.

Collects errors and warnings during discovery and provides structured
feedback for downstream consumption.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ErrorHandler:
    """Centralised error and warning collector for connectors.

    Args:
        connector_name: Name of the connector reporting errors.
    """

    def __init__(self, connector_name: str) -> None:
        self._name = connector_name
        self._errors: list[dict[str, str]] = []
        self._warnings: list[str] = []

    def add_error(self, operation: str, message: str) -> None:
        """Record a fatal error with its operation context.

        Args:
            operation: Name of the operation that failed.
            message: Human-readable error description.
        """
        self._errors.append({"operation": operation, "message": message})
        logger.error("[%s] Error in %s: %s", self._name, operation, message)

    def add_warning(self, message: str) -> None:
        """Record a non-fatal warning.

        Args:
            message: Human-readable warning description.
        """
        self._warnings.append(message)
        logger.warning("[%s] %s", self._name, message)

    @property
    def has_errors(self) -> bool:
        """Return True if any errors were recorded."""
        return len(self._errors) > 0

    @property
    def errors(self) -> list[dict[str, str]]:
        """Return a copy of all recorded errors."""
        return self._errors.copy()

    @property
    def warnings(self) -> list[str]:
        """Return a copy of all recorded warnings."""
        return self._warnings.copy()

    def summary(self) -> dict[str, Any]:
        """Return a summary of all errors and warnings.

        Returns:
            Dictionary with counts and lists of errors/warnings.
        """
        return {
            "connector": self._name,
            "error_count": len(self._errors),
            "warning_count": len(self._warnings),
            "errors": self._errors,
            "warnings": self._warnings,
        }

    def clear(self) -> None:
        """Clear all recorded errors and warnings."""
        self._errors.clear()
        self._warnings.clear()
