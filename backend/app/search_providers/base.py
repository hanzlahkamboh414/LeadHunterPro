"""Base interface for all search providers.

Every search provider must implement this abstract base class.
Providers can query public search engines (SearXNG, Brave) or
custom APIs.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.search_providers.models import SearchQuery, SearchResponse

logger = logging.getLogger(__name__)


class BaseSearchProvider(ABC):
    """Abstract base class for search providers.

    All providers must implement :meth:`search` which performs an
    actual web query and returns structured results.

    Subclasses are responsible for their own rate limiting, retries,
    and error handling — the manager only coordinates fallbacks.

    Attributes:
        provider_name: Unique identifier used by the provider registry.
        description: Human-readable one-liner for logs and diagnostics.
        enabled: Whether this provider is active (controlled by config).
        priority: Execution order among providers (lower = tried first).
    """

    provider_name: str = ""
    description: str = ""
    enabled: bool = True
    priority: int = 100

    @abstractmethod
    async def search(self, query: SearchQuery) -> SearchResponse:
        """Execute a search query against this provider.

        Args:
            query: The search query to execute.

        Returns:
            A SearchResponse containing results and metadata.

        Raises:
            Does NOT raise — all errors are captured in SearchResponse.error.
        """
        ...

    async def health_check(self) -> dict[str, Any]:
        """Lightweight health check for this provider.

        Returns:
            Dict with 'healthy' (bool) and optional diagnostic info.
        """
        return {"healthy": self.enabled, "provider": self.provider_name}

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__}"
            f" name={self.provider_name!r}"
            f" enabled={self.enabled}>"
        )
