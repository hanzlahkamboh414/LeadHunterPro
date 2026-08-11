"""Main async HTTP crawler orchestrating all infrastructure components.

HTTPCrawler combines session management, rate limiting, retry logic,
robots.txt compliance, caching, and HTML parsing into a single
cohesive interface for connector use.

Usage:
    async with HTTPCrawler(config) as crawler:
        response = await crawler.crawl(CrawlRequest(url="https://example.com"))
        parsed = crawler.parser.parse(response.text, response.url)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Self

from app.crawlers.base import BaseCrawler, CrawlRequest
from app.crawlers.cache import ResponseCache
from app.crawlers.config import CrawlerConfig
from app.crawlers.exceptions import (
    CrawlerHTTPError,
    CrawlerRobotsBlocked,
    CrawlerTimeout,
)
from app.crawlers.html_parser import HTMLParser
from app.crawlers.rate_limiter import RateLimiter
from app.crawlers.response import CrawlResponse
from app.crawlers.retry import RetryEngine
from app.crawlers.robots import RobotsManager
from app.crawlers.session_manager import SessionManager

logger = logging.getLogger(__name__)


class HTTPCrawler(BaseCrawler):
    """Production-grade async HTTP crawler with full infrastructure.

    Combines all crawler components into a single orchestrator:
    - Session management with connection pooling
    - Per-host and global rate limiting
    - Exponential backoff retry on transient failures
    - robots.txt compliance checking
    - TTL-based response caching
    - HTML parsing utilities
    """

    def __init__(self, config: CrawlerConfig | None = None) -> None:
        """Initialize the HTTP crawler.

        Args:
            config: Crawler configuration. Uses defaults if None.
        """
        self._config = config or CrawlerConfig()
        self._session_manager = SessionManager()
        self._rate_limiter = RateLimiter(
            per_host_delay=self._config.rate_limit_per_host_seconds,
            global_delay=self._config.rate_limit_global_seconds,
        )
        self._retry_engine = RetryEngine(
            max_retries=self._config.max_retries,
            backoff_base=self._config.retry_backoff_base,
            backoff_max=self._config.retry_backoff_max,
        )
        self._cache = ResponseCache(default_ttl=600)
        self._robots = RobotsManager(cache=self._cache)
        self._parser = HTMLParser()
        self._user_agent_index = 0

    @property
    def config(self) -> CrawlerConfig:
        """Get the crawler configuration."""
        return self._config

    @property
    def cache(self) -> ResponseCache:
        """Get the response cache."""
        return self._cache

    @property
    def parser(self) -> HTMLParser:
        """Get the HTML parser instance."""
        return self._parser

    @property
    def rate_limiter(self) -> RateLimiter:
        """Get the rate limiter instance."""
        return self._rate_limiter

    async def crawl(self, request: CrawlRequest) -> CrawlResponse:
        """Execute a crawl request with full infrastructure.

        Args:
            request: The crawl request to execute.

        Returns:
            CrawlResponse with the fetched content.

        Raises:
            CrawlerRobotsBlocked: If robots.txt blocks the URL.
            CrawlerTimeout: If the request times out.
            CrawlerHTTPError: If an HTTP error occurs.
            CrawlerRetryExceeded: If all retries are exhausted.
        """
        start_time = time.monotonic()
        url = request.url
        parsed_url = self._parse_url(url)
        host = parsed_url.netloc.lower()

        # Check robots.txt
        if request.respect_robots and self._config.respect_robots_txt:
            directive = await self._robots.is_allowed(url)
            if not directive.allowed:
                logger.warning("Robots.txt blocked: %s (%s)", url, directive.reason)
                raise CrawlerRobotsBlocked(url, directive.reason)

        # Apply rate limiting
        self._rate_limiter.acquire(host)

        # Check cache
        cache_key = f"url:{url}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for %s", url)
            response = cached
            response = replace(response, cached=True)
            return response

        # Execute with retry
        try:
            response = await self._fetch_with_retry(request, host)
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.monotonic() - start_time) * 1000
            return CrawlResponse(
                url=url,
                status_code=0,
                content=b"",
                headers={},
                successful=False,
                robots_compliant=True,
                error=str(exc),
                response_time_ms=elapsed_ms,
            )

        elapsed_ms = (time.monotonic() - start_time) * 1000
        response = replace(response, response_time_ms=elapsed_ms)

        # Cache successful responses
        if response.successful:
            self._cache.set(cache_key, response, ttl_seconds=600)

        return response

    async def _fetch_with_retry(
        self,
        request: CrawlRequest,
        host: str,
    ) -> CrawlResponse:
        """Fetch a URL with automatic retry on transient failures.

        Args:
            request: The crawl request.
            host: The target hostname.

        Returns:
            CrawlResponse with the fetched content.

        Raises:
            CrawlerTimeout: If the request times out.
            CrawlerHTTPError: If an HTTP error occurs.
        """
        import aiohttp

        user_agent = self._get_user_agent()

        async def _do_fetch() -> CrawlResponse:
            session = self._session_manager.session
            headers = {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                **request.headers,
            }

            try:
                async with session.get(
                    request.url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=request.timeout),
                    allow_redirects=request.follow_redirects,
                ) as response:
                    content = await response.read()

                    return CrawlResponse(
                        url=str(response.url),
                        status_code=response.status,
                        content=content,
                        headers=dict(response.headers),
                        successful=200 <= response.status < 300,
                        robots_compliant=True,
                    )

            except asyncio.TimeoutError:
                raise CrawlerTimeout(f"Request to {request.url} timed out")
            except aiohttp.ClientResponseError as exc:
                if exc.status >= 500:
                    raise  # Server errors are retried
                raise CrawlerHTTPError(
                    status_code=exc.status,
                    url=request.url,
                )
            except aiohttp.ClientError:
                raise  # Connection errors are retried

        try:
            return await self._retry_engine.execute(_do_fetch)
        except (CrawlerTimeout, CrawlerHTTPError):
            raise
        except Exception as exc:
            raise CrawlerHTTPError(
                status_code=0,
                url=request.url,
                message=str(exc),
            ) from exc

    def _get_user_agent(self) -> str:
        """Get the next User-Agent from the rotation pool.

        Returns:
            A User-Agent string.
        """
        ua_pool = self._config.user_agent_pool
        ua = ua_pool[self._user_agent_index % len(ua_pool)]
        self._user_agent_index += 1
        return ua

    @staticmethod
    def _parse_url(url: str) -> Any:
        """Parse a URL string.

        Args:
            url: The URL to parse.

        Returns:
            urllib.parse.ParseResult object.
        """
        from urllib.parse import urlparse

        return urlparse(url)

    async def close(self) -> None:
        """Clean up resources."""
        await self._session_manager.close()
        logger.debug("HTTPCrawler closed")

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        """Async context manager exit."""
        await self.close()
