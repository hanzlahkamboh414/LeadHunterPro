"""Base interface for all discovery sources.

Every source must implement this ABC. Sources can be:
- Live web sources (search APIs, public portals, government registries)
- Directory scrapers (trade associations, directories)
- Database lookups (license registries, procurement databases)
- Fixture/bridge data (emergency fallback only)

Sources are executed by SourceOrchestrator in priority order.
Each source operates independently — the orchestrator aggregates results.

Status contract (every discover() MUST return one of these):
  SUCCESS  — found companies
  EMPTY    — executed but found nothing
  UNAVAILABLE — source reachable but returned no data / timed out
  ERROR    — unexpected exception during execution
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.discovery.sources.status import SourceStatus

logger = logging.getLogger(__name__)


class BaseSource(ABC):
    """Abstract base class for all discovery sources.

    Attributes:
        source_name: Unique identifier (e.g. "sos_business").
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
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        """Execute discovery for this source.

        Args:
            industry: Industry keyword (e.g. "Roofing").
            location: Geographic location (e.g. "Dallas Texas").
            limit: Maximum results to return.

        Returns:
            Tuple of (status, companies, metadata).
            Status is one of SUCCESS / EMPTY / UNAVAILABLE / ERROR.
            On error, return (ERROR, [], {"error": str}).
        """
        ...

    async def health_check(self) -> dict[str, Any]:
        """Lightweight health check.

        Returns:
            Dict with 'healthy' (bool), 'source', 'status', and optional
            diagnostic info.
        """
        return {
            "healthy": self.enabled,
            "source": self.source_name,
            "status": "active" if self.enabled else "disabled",
        }

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__}"
            f" name={self.source_name!r}"
            f" priority={self.priority}"
            f" enabled={self.enabled}>"
        )
