"""Tavily Search API provider implementation.

Tavily is a production search API designed for AI agents — clean JSON, a
generous free tier (~1,000 searches/month), and no result-count surprises.
This provider is the Inc10 feeder: with a real key it is the web-search
start point the discovery pipeline needs to find candidate company
websites, which :class:`WebsiteEnricher` then locates from page evidence.

Configuration via environment:
    TAVILY_SEARCH_API_KEY=tvly-...   # Required: Tavily API key

Contract (Tavily docs):
    POST https://api.tavily.com/search
    body: {"api_key", "query", "max_results": <=20, "search_depth": "basic"}
    response: {"results": [{"title", "url", "content", "score", ...}], ...}

Nothing is fabricated: an auth/HTTP/parse failure is returned as an honest
``SearchResponse(status="error")`` exactly like the SearXNG and Brave
providers, and results without a URL or title are skipped (§12).
"""

from __future__ import annotations

import logging
import os
import time
from types import TracebackType
from typing import Any

from app.search_providers.base import BaseSearchProvider
from app.search_providers.models import SearchQuery, SearchResponse, SearchResult

logger = logging.getLogger(__name__)

#: Tavily caps each request at 20 results. Kept as a module constant so a
#: future API change is a one-line fix, not a search-and-replace.
_ENDPOINT = "https://api.tavily.com/search"
_MAX_RESULTS = 20


class TavilySearchProvider(BaseSearchProvider):
    """Search provider backed by the Tavily Search API.

    Requires TAVILY_SEARCH_API_KEY environment variable.
    Falls back gracefully (honest error) when no key is configured.
    """

    provider_name = "tavily"
    description = "Tavily Search API"
    priority = 20

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: int = 10,
        max_results: int = 20,
        endpoint: str = _ENDPOINT,
    ) -> None:
        """Initialize the Tavily Search provider.

        Args:
            api_key: Tavily API key. Defaults to TAVILY_SEARCH_API_KEY env var.
            timeout: Request timeout in seconds.
            max_results: Maximum results to request per query (Tavily caps at 20).
            endpoint: Tavily API endpoint (overridable for tests/proxies).
        """
        self._api_key = api_key or self._load_api_key()
        self._timeout = timeout
        self._max_results = min(max(max_results, 1), _MAX_RESULTS)
        self._endpoint = endpoint
        self._session: Any = None

    @staticmethod
    def _load_api_key() -> str | None:
        """Load the API key from the environment."""
        return os.environ.get("TAVILY_SEARCH_API_KEY")

    async def _get_session(self) -> Any:
        """Get or create an aiohttp session (lazy initialization)."""
        import aiohttp

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def search(self, query: SearchQuery) -> SearchResponse:
        """Execute a search query against the Tavily API.

        Args:
            query: The search query.

        Returns:
            A SearchResponse with parsed results (or an honest error).
        """
        if not self._api_key:
            logger.debug("Tavily: no API key configured, skipping")
            return SearchResponse(
                provider=self.provider_name,
                query=query.keywords,
                error="No API key configured (set TAVILY_SEARCH_API_KEY)",
                status="error",
            )

        start = time.monotonic()

        try:
            session = await self._get_session()
            payload = self._build_payload(query)
            headers = {"Content-Type": "application/json", "Accept": "application/json"}

            logger.debug("Tavily query: %s", payload.get("query"))

            async with session.post(
                self._endpoint, json=payload, headers=headers
            ) as resp:
                if resp.status in (401, 403):
                    raise TavilyAuthError(
                        f"Tavily API authentication failed (HTTP {resp.status})"
                    )
                if resp.status != 200:
                    body = await resp.text()
                    raise TavilyAPIError(
                        status=resp.status,
                        message=f"Tavily API returned HTTP {resp.status}: {body[:200]}",
                    )

                data = await resp.json()
                elapsed = (time.monotonic() - start) * 1000

                results = self._parse_results(data, query)
                return SearchResponse(
                    results=results,
                    provider=self.provider_name,
                    query=query.keywords,
                    total_estimated=len(results),
                    latency_ms=elapsed,
                    status="success" if results else "partial",
                )

        except TavilyAuthError as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("Tavily auth error: %s", exc)
            return SearchResponse(
                provider=self.provider_name,
                query=query.keywords,
                latency_ms=elapsed,
                error=str(exc),
                status="error",
            )
        except TavilyAPIError as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("Tavily API error: %s", exc)
            return SearchResponse(
                provider=self.provider_name,
                query=query.keywords,
                latency_ms=elapsed,
                error=str(exc),
                status="error",
            )
        except Exception as exc:  # translated, never raised (§12)
            elapsed = (time.monotonic() - start) * 1000
            logger.exception("Tavily search failed")
            return SearchResponse(
                provider=self.provider_name,
                query=query.keywords,
                latency_ms=elapsed,
                error=f"Tavily error: {exc}",
                status="error",
            )

    def _build_payload(self, query: SearchQuery) -> dict[str, Any]:
        """Build the JSON request body.

        The location hint is appended to the keywords (Tavily has no
        geo-parameter); this mirrors how SearXNG folds location into ``q``.
        """
        keywords = (
            f"{query.keywords} {query.location}".strip()
            if query.location
            else query.keywords
        )
        # Honor the caller's num_results, bounded by this provider's configured
        # cap and Tavily's hard 20-result API cap — never ask for more than the
        # caller wants, and never exceed the API limit.
        requested = query.num_results or self._max_results
        return {
            "api_key": self._api_key,
            "query": keywords,
            "max_results": min(requested, self._max_results, _MAX_RESULTS),
            "search_depth": "basic",
        }

    @staticmethod
    def _parse_results(
        data: dict[str, Any], query: SearchQuery
    ) -> list[SearchResult]:
        """Parse Tavily JSON response into standardized results.

        Args:
            data: The parsed JSON response from the Tavily API.
            query: The original query (for position assignment).

        Returns:
            List of SearchResult objects.
        """
        results: list[SearchResult] = []
        items = data.get("results", [])

        for idx, item in enumerate(items[: query.num_results], start=1):
            url = item.get("url", "")
            title = item.get("title", "")
            content = item.get("content", "")
            if not url or not title:
                continue

            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=content,
                    position=idx,
                )
            )

        logger.info("Tavily returned %d results for %r", len(results), query.keywords)
        return results


class TavilyAuthError(Exception):
    """Raised when Tavily API authentication fails."""


class TavilyAPIError(Exception):
    """Raised when Tavily API returns a non-successful status."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)