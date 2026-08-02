"""Construction Source Connectors – framework and implementations.

Provides a composable connector interface for discovering construction
companies from industry-specific public sources (procurement portals,
trade directories, bid databases).

No Google/Bing/DuckDuckGo – only purpose-built construction sources.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.engines.source_connectors.texas_procurement import TexasProcurementConnector

logger = logging.getLogger(__name__)


class ConstructionSourceConnector(ABC):
    """Base class for all construction source connectors."""

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

    def is_available(self) -> bool:
        """Return True if this connector can run in the current environment."""
        return True


class ConstructionSourceRegistry:
    """Registry that holds all available construction source connectors."""

    _connectors: dict[str, ConstructionSourceConnector] = {}

    @classmethod
    def register(cls, connector: ConstructionSourceConnector) -> None:
        """Register a connector instance."""
        cls._connectors[connector.name] = connector
        logger.info("Registered connector: %s (%s)", connector.name, connector.description)

    @classmethod
    def get(cls, name: str) -> ConstructionSourceConnector | None:
        """Get a connector by name."""
        return cls._connectors.get(name)

    @classmethod
    def list_all(cls) -> list[ConstructionSourceConnector]:
        """Return all registered connectors."""
        return list(cls._connectors.values())

    @classmethod
    def list_names(cls) -> list[str]:
        """Return names of all registered connectors."""
        return list(cls._connectors.keys())


# Register built-in connectors
ConstructionSourceRegistry.register(TexasProcurementConnector())


def get_connector(name: str) -> ConstructionSourceConnector | None:
    """Convenience function to look up a connector by name."""
    return ConstructionSourceRegistry.get(name)


def list_connectors() -> list[dict[str, str]]:
    """Return a summary of all registered connectors."""
    return [
        {"name": c.name, "description": c.description}
        for c in ConstructionSourceRegistry.list_all()
    ]
