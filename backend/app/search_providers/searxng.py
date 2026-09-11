"""SearXNG search provider implementation.

SearXNG is a free, self-hostable metasearch engine that aggregates
results from multiple sources without requiring an API key.

Configuration via environment:
    SEARXNG_URL=https://searxng.example.com   # Required: your SearXNG instance
    SEARXNG_TIMEOUT=6                          # Optional: request timeout (default 6s)
    SEARXNG_MAX_RESULTS=20                     # Optional: max results per query
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from app.search_providers.base import BaseSearchProvider, LoopSessionMixin
from app.search_providers.models import SearchQuery, SearchResponse, SearchResult

logger = logging.getLogger(__name__)


class SearXNGProvider(BaseSearchProvider, LoopSessionMixin):
    """Search provider backed by a SearXNG instance.

    SearXNG returns JSON-formatted results when queried with the right
    parameters. This provider handles the query construction, response
    parsing, and error translation.

    Example query:
        https://searxng.example.com/search?q=roofing+dallas+tx&format=json&categories=general
    """

    provider_name = "searxng"
    description = "Self-hosted SearXNG metasearch engine"
    priority = 10

    # Global throttle: SearXNG fans every query out to the upstream engines,
    # and a burst from the pipeline suspends them (CAPTCHA / 429 — measured
    # on a datacenter IP). Spacing queries ~5 qps keeps the engine pool
    # healthy under multi-user load. CLASS-level (threading.Lock + monotonic
    # reservation slots, mirroring brave.py) because the pipeline runs
    # several concurrent event loops — a per-instance or asyncio.Lock would
    # only pace one loop.
    _RATE_INTERVAL_S = 0.2
    _rate_lock: threading.Lock = threading.Lock()
    _rate_next_slot: float = 0.0

    def __init__(
        self,
        base_url: str,
        *,
        timeout: int = 6,
        max_results: int = 20,
        safe_search: bool = True,
    ) -> None:
        """Initialize the SearXNG provider.

        Args:
            base_url: Full URL to the SearXNG instance (e.g. 'https://searx.org').
            timeout: Request timeout in seconds (SEARXNG_TIMEOUT; a per-query
                hard cap so a slow instance cannot stall a whole run).
            max_results: Maximum number of results to request.
            safe_search: Whether to enable safe search filtering.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # Manager hard-gate must cover BOTH the aiohttp request cap AND the
        # rate-limiter queue wait — a backstop that fires while a query is
        # waiting for its slot blacklists a healthy provider (brave.py, 3ca6022).
        # 30s budget: two aligned job bursts measured ~70+ queued queries
        # (0.2s each = 14s wait) before the old 21s gate cancelled them and
        # the breaker opened a 5-minute Tavily blackout.
        self.timeout_s = float(timeout) + 30.0
        self._max_results = max_results
        self._safe_search = 1 if safe_search else 0
        # LoopSessionMixin: one session per event loop (concurrency fix —
        # see the mixin docstring in base.py).
        self._init_sessions()

    async def _respect_rate_limit(self) -> None:
        """Reserve a slot on the global rate clock and wait for it.

        Reservation happens under a threading.Lock (atomic check-and-book),
        then the sleep happens OUTSIDE the lock so waiting callers don't
        block the thread that owns the next slot.
        """
        with SearXNGProvider._rate_lock:
            now = time.monotonic()
            slot = max(now, SearXNGProvider._rate_next_slot)
            SearXNGProvider._rate_next_slot = (
                slot + SearXNGProvider._RATE_INTERVAL_S
            )
        wait = slot - now
        if wait > 10.0:
            logger.warning(
                "SearXNG rate-queue backlog: next slot in %.1fs "
                "(query demand exceeds throttle drain)",
                wait,
            )
        if wait > 0:
            await asyncio.sleep(wait)

    async def __aenter__(self) -> SearXNGProvider:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def search(self, query: SearchQuery) -> SearchResponse:
        """Execute a search query against SearXNG.

        Args:
            query: The search query to execute.

        Returns:
            A SearchResponse with parsed results.
        """
        await self._respect_rate_limit()
        start = time.monotonic()

        try:
            session = await self._get_session()
            url = self._build_url(query)
            logger.debug("SearXNG query: %s", url)

            async with session.get(url) as resp:
                if resp.status != 200:
                    raise SearXNGHTTPError(
                        status=resp.status,
                        message=f"SearXNG returned HTTP {resp.status}",
                    )

                data = await resp.json()
                elapsed = (time.monotonic() - start) * 1000

                results = self._parse_results(data, query)
                total = data.get("number_of_results", len(results))

                return SearchResponse(
                    results=results,
                    provider=self.provider_name,
                    query=query.keywords,
                    total_estimated=total,
                    latency_ms=elapsed,
                    status="success" if results else "partial",
                )

        except SearXNGHTTPError as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("SearXNG HTTP error: %s", exc)
            return SearchResponse(
                provider=self.provider_name,
                query=query.keywords,
                latency_ms=max(elapsed, 0.1),
                error=str(exc),
                status="error",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.exception("SearXNG search failed: %s")
            return SearchResponse(
                provider=self.provider_name,
                query=query.keywords,
                latency_ms=elapsed,
                error=f"SearXNG error: {exc}",
                status="error",
            )

    def _build_url(self, query: SearchQuery) -> str:
        """Build the SearXNG JSON API URL.

        Args:
            query: The search query.

        Returns:
            The full URL string.
        """
        params: dict[str, Any] = {
            "q": query.keywords,
            "format": "json",
            "categories": "general",
            "safesearch": self._safe_search,
            "language": "en",
        }
        if query.location:
            params["q"] = f"{query.keywords} {query.location}"
        if query.num_results > 0:
            params["pageno"] = 1
            params["topics"] = "general"

        from urllib.parse import urlencode

        return f"{self._base_url}/search?{urlencode(params)}"

    @staticmethod
    def _parse_results(data: dict[str, Any], query: SearchQuery) -> list[SearchResult]:
        """Parse SearXNG JSON response into standardized results.

        Args:
            data: The parsed JSON response from SearXNG.
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

        logger.info("SearXNG returned %d results for %r", len(results), query.keywords)
        return results


class SearXNGHTTPError(Exception):
    """Raised when SearXNG returns a non-2xx status."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


class SearXNGConfigError(Exception):
    """Raised when SearXNG configuration is invalid."""
