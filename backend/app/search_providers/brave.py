"""Brave Search API provider implementation.

Brave Search offers a REST API with generous free-tier limits
(2,000 queries/month). Requires an API key configured via
BRAVE_SEARCH_API_KEY environment variable.

Documentation: https://brave.com/search/api/
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.search_providers.base import BaseSearchProvider, LoopSessionMixin
from app.search_providers.models import SearchQuery, SearchResponse, SearchResult

logger = logging.getLogger(__name__)


class BraveSearchProvider(BaseSearchProvider, LoopSessionMixin):
    """Search provider backed by the Brave Search API.

    Requires BRAVE_SEARCH_API_KEY environment variable.
    Falls back gracefully if no key is configured.
    """

    provider_name = "brave"
    description = "Brave Search REST API"
    priority = 20

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: int = 10,
        max_results: int = 20,
    ) -> None:
        """Initialize the Brave Search provider.

        Args:
            api_key: Brave Search API key. Defaults to BRAVE_SEARCH_API_KEY env var.
            timeout: Request timeout in seconds.
            max_results: Maximum results to request per query.
        """
        self._api_key = api_key or self._load_api_key()
        self._timeout = timeout
        self._max_results = min(max(max_results, 1), 50)  # Brave limits to 50
        self._endpoint = "https://api.search.brave.com/res/v1/web/search"
        # LoopSessionMixin: one session per event loop (concurrency fix —
        # see the mixin docstring in base.py).
        self._init_sessions()

    @staticmethod
    def _load_api_key() -> str | None:
        """Load API key from environment variable."""
        import os

        return os.environ.get("BRAVE_SEARCH_API_KEY")

    async def __aenter__(self) -> BraveSearchProvider:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def search(self, query: SearchQuery) -> SearchResponse:
        """Execute a search query against Brave Search API.

        Args:
            query: The search query to execute.

        Returns:
            A SearchResponse with parsed results.
        """
        # No API key = skip this provider silently
        if not self._api_key:
            logger.debug("Brave Search: no API key configured, skipping")
            return SearchResponse(
                provider=self.provider_name,
                query=query.keywords,
                error="No API key configured (set BRAVE_SEARCH_API_KEY)",
                status="error",
            )

        start = time.monotonic()

        try:
            session = await self._get_session()
            url = self._endpoint
            params = self._build_params(query)
            headers = self._build_headers()

            logger.debug("Brave Search query: %s", params.get("q"))

            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 401 or resp.status == 403:
                    raise BraveAuthError(
                        f"Brave API authentication failed (HTTP {resp.status})"
                    )
                if resp.status != 200:
                    body = await resp.text()
                    raise BraveAPIError(
                        status=resp.status,
                        message=f"Brave API returned HTTP {resp.status}: {body[:200]}",
                    )

                data = await resp.json()
                elapsed = (time.monotonic() - start) * 1000

                results = self._parse_results(data, query)
                total = data.get("total", len(results))

                return SearchResponse(
                    results=results,
                    provider=self.provider_name,
                    query=query.keywords,
                    total_estimated=total,
                    latency_ms=elapsed,
                    status="success" if results else "partial",
                )

        except BraveAuthError as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("Brave Search auth error: %s", exc)
            return SearchResponse(
                provider=self.provider_name,
                query=query.keywords,
                latency_ms=elapsed,
                error=str(exc),
                status="error",
            )
        except BraveAPIError as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("Brave Search API error: %s", exc)
            return SearchResponse(
                provider=self.provider_name,
                query=query.keywords,
                latency_ms=elapsed,
                error=str(exc),
                status="error",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.exception("Brave Search failed: %s")
            return SearchResponse(
                provider=self.provider_name,
                query=query.keywords,
                latency_ms=elapsed,
                error=f"Brave Search error: {exc}",
                status="error",
            )

    def _build_params(self, query: SearchQuery) -> dict[str, str]:
        """Build query parameters for the Brave API.

        Args:
            query: The search query.

        Returns:
            Dict of URL parameters.
        """
        params: dict[str, str] = {
            "q": query.keywords,
            "count": str(min(self._max_results, 50)),
        }
        if query.location:
            params["search_lang"] = "en"
            # Brave uses `regions` param for geo-targeting
            params["regions"] = "us-en"
        return params

    def _build_headers(self) -> dict[str, str]:
        """Build request headers including API key."""
        return {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
            "User-Agent": "LeadHunterPro/1.0 (contact: leads@leadhunterpro.com)",
        }

    @staticmethod
    def _parse_results(data: dict[str, Any], query: SearchQuery) -> list[SearchResult]:
        """Parse Brave API JSON response into standardized results.

        Args:
            data: The parsed JSON response from Brave API.
            query: The original query.

        Returns:
            List of SearchResult objects.
        """
        results: list[SearchResult] = []
        web_results = data.get("web", {}).get("results", [])

        for idx, item in enumerate(web_results[: query.num_results], start=1):
            url = item.get("url", "")
            title = item.get("title", "")
            description = item.get("description", "")

            if not url or not title:
                continue

            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=description,
                    position=idx,
                )
            )

        logger.info(
            "Brave Search returned %d results for %r", len(results), query.keywords
        )
        return results


class BraveAuthError(Exception):
    """Raised when Brave API authentication fails."""


class BraveAPIError(Exception):
    """Raised when Brave API returns a non-successful status."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)
