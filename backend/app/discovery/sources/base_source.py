"""Base interface for all discovery sources.

Every source must implement this ABC. Sources can be:
- Live web sources (search APIs, public portals)
- Directory scrapers (trade associations, directories)
- Database lookups (license registries, procurement databases)
- Fixture/bridge data (emergency fallback only)

Sources are executed by SourceOrchestrator in priority order.
Each source operates independently — the orchestrator aggregates results.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class BaseSource(ABC):
    """Abstract base class for all discovery sources.

    Attributes:
        source_name: Unique identifier (e.g. "searxng", "fixture_bridge").
        description: Human-readable one-liner.
        priority: Execution order (lower = tried first).
        enabled: Whether this source is active.
    """

    source_name: str = ""
    description: str = ""
    priority: int = 100
    enabled: bool = True

    @abstractmethod
    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Execute discovery for this source.

        Args:
            industry: Industry keyword (e.g. "Roofing").
            location: Geographic location (e.g. "Dallas Texas").
            limit: Maximum results to return.

        Returns:
            Tuple of (list of company dicts, metadata dict).
            On failure, return ([], {"error": str}) — never raise.
        """
        ...

    async def health_check(self) -> dict[str, Any]:
        """Lightweight health check.

        Returns:
            Dict with 'healthy' (bool) and optional diagnostic info.
        """
        return {"healthy": self.enabled, "source": self.source_name}

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__}"
            f" name={self.source_name!r}"
            f" priority={self.priority}"
            f" enabled={self.enabled}>"
        )
