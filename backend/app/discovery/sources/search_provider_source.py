"""Search provider source — wraps SearchProviderManager as a BaseSource.

Allows SearXNG and Brave Search to be used as ONE source among many
in the SourceOrchestrator pipeline. If no providers are configured,
returns empty results gracefully (does not crash).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.discovery.sources.base_source import BaseSource
from app.discovery.sources.status import SourceStatus
from app.search_providers.manager import SearchProviderManager
from app.search_providers.models import SearchQuery

logger = logging.getLogger(__name__)


class SearchProviderSource(BaseSource):
    """Wraps SearchProviderManager as a SourceOrchestrator-compatible source.

    This source attempts live web search via configured providers
    (SearXNG, Brave). Falls back to empty results if no providers
    are registered or all fail.
    """

    source_name = "search_providers"
    description = "Web search via SearXNG / Brave API (optional)"
    priority = 50
    enabled = True

    def __init__(self) -> None:
        """Initialize the search provider source."""
        self._manager: SearchProviderManager | None = None

    def _ensure_manager(self) -> None:
        """Lazy-initialize the SearchProviderManager."""
        if self._manager is None:
            self._manager = SearchProviderManager()

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Execute search via registered providers.

        Args:
            industry: Industry keyword.
            location: Geographic location.
            limit: Maximum results.

        Returns:
            Tuple of (company dicts, metadata).
            Returns ([], {...}) on any failure — never raises.
        """
        self._ensure_manager()

        from app.search_providers.registry import get_registry

        registry = get_registry()
        enabled = registry.get_enabled()

        if not enabled:
            logger.debug(
                "SearchProviderSource: no providers registered "
                "(SEARXNG_URL and BRAVE_SEARCH_API_KEY not set)"
            )
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "error": "no_providers_configured",
                "providers_available": [],
            }

        from app.connectors.texas_procurement import _parse_location

        city, state = _parse_location(location)
        query_text = f"{industry} contractor {city or ''} {state}".strip()

        logger.info(
            "SearchProviderSource: searching %r with %d providers",
            query_text,
            len(enabled),
        )

        query = SearchQuery(keywords=query_text, num_results=limit * 3)

        try:
            response = asyncio.run(self._manager.search(query))
        except Exception as exc:  # noqa: BLE001
            logger.error("SearchProviderSource: search exception: %s", exc)
            return SourceStatus.ERROR, [], {
                "source": self.source_name,
                "error": str(exc),
                "providers_tried": [p.provider_name for p in enabled],
            }

        logger.info(
            "SearchProviderSource: status=%r results=%d error=%r",
            response.status,
            len(response.results),
            response.error[:100] if response.error else "",
        )

        if not response.results:
            reason = response.error or "no_results"
            logger.debug("SearchProviderSource: no results — %s", reason)
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "status": response.status,
                "error": reason,
                "provider": response.provider,
            }

        # Convert to company dicts for downstream processing
        companies: list[dict[str, Any]] = []
        for result in response.results:
            companies.append(
                {
                    "company_name": result.title,
                    "website": result.url,
                    "city": city or "",
                    "state": state,
                    "country": "USA",
                    "trade_category": "",  # Classified downstream
                    "industry_focus": result.snippet or result.title,
                    "revenue_tier": "",
                    "source_url": result.url,
                    "data_provenance": f"live:{result.position}",
                    "discovery_reason": "",
                }
            )

        return SourceStatus.SUCCESS, companies, {
            "source": self.source_name,
            "status": response.status,
            "results_count": len(companies),
            "provider": response.provider,
            "latency_ms": response.latency_ms,
            "error": response.error or "",
        }

    async def health_check(self) -> dict[str, Any]:
        """Check if any search providers are registered and healthy."""
        from app.search_providers.registry import get_registry

        registry = get_registry()
        enabled = registry.get_enabled()
        return {
            "healthy": len(enabled) > 0,
            "source": self.source_name,
            "providers_registered": len(enabled),
            "provider_names": [p.provider_name for p in enabled],
        }
