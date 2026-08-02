"""Base connector interface for Construction Source Connectors."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class ConstructionSourceConnector(ABC):
    """Abstract base class for construction source connectors.

    Each connector discovers construction companies from a specific
    public source (procurement portal, trade directory, bid database).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable connector name (e.g. 'texas_procurement')."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """One-line description of what this connector discovers."""
        ...

    def is_available(self) -> bool:
        """Return True if this connector can run in the current environment."""
        return True

    @abstractmethod
    def discover(
        self,
        *,
        state: str | None = None,
        city: str | None = None,
        industry: str = "Construction Estimating",
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Execute discovery and return (companies, metadata).

        Args:
            state: US state code (e.g. 'TX').
            city: City name.
            industry: Industry filter.
            limit: Maximum companies to return.

        Returns:
            (list of company dicts, metadata dict)
        """
        ...
