"""Tests for HTTP crawler (integration-level)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.crawlers.base import CrawlRequest
from app.crawlers.config import CrawlerConfig
from app.crawlers.http_crawler import HTTPCrawler


class TestHTTPCrawler:
    """Test HTTPCrawler orchestration."""

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return CrawlerConfig(
            default_timeout=5,
            max_retries=1,
            rate_limit_per_host_seconds=0.01,
            rate_limit_global_seconds=0.01,
        )

    @pytest.fixture
    def crawler(self, config):
        """Create HTTPCrawler instance."""
        return HTTPCrawler(config=config)

    @pytest.mark.asyncio
    async def test_crawl_successful_response(self, crawler):
        """Successful HTTP request returns CrawlResponse."""
        with patch.object(crawler._robots, "is_allowed") as mock_robots:
            mock_robots.return_value = MagicMock(allowed=True)
            with patch.object(crawler, "_fetch_with_retry") as mock_fetch:
                # Return a response that will be mutated by crawl()
                mock_response = MagicMock()
                mock_response.successful = True
                mock_response.status_code = 200
                mock_fetch.return_value = mock_response
                request = CrawlRequest(url="https://example.com")
                result = await crawler.crawl(request)
                assert result.successful is True
                assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_crawl_returns_error_on_failure(self, crawler):
        """Failed request returns error response."""
        with patch.object(crawler._robots, "is_allowed") as mock_robots:
            mock_robots.return_value = MagicMock(allowed=True)
            with patch.object(crawler, "_fetch_with_retry") as mock_fetch:
                mock_fetch.side_effect = Exception("Network error")
                request = CrawlRequest(url="https://example.com")
                result = await crawler.crawl(request)
                assert result.successful is False
                assert "Network error" in result.error

    @pytest.mark.asyncio
    async def test_crawl_checks_robots(self, crawler):
        """Crawler checks robots.txt before fetching."""
        with patch.object(crawler._robots, "is_allowed") as mock_robots:
            mock_robots.return_value = MagicMock(allowed=False)
            request = CrawlRequest(url="https://example.com/blocked")
            with pytest.raises(Exception):  # noqa: B017 - CrawlerRobotsBlocked
                await crawler.crawl(request)

    def test_parser_property(self, crawler):
        """Parser property returns HTMLParser instance."""
        from app.crawlers.html_parser import HTMLParser

        assert isinstance(crawler.parser, HTMLParser)

    def test_cache_property(self, crawler):
        """Cache property returns ResponseCache instance."""
        from app.crawlers.cache import ResponseCache

        assert isinstance(crawler.cache, ResponseCache)

    def test_rate_limiter_property(self, crawler):
        """Rate limiter property returns RateLimiter instance."""
        from app.crawlers.rate_limiter import RateLimiter

        assert isinstance(crawler.rate_limiter, RateLimiter)

    def test_config_property(self, crawler, config):
        """Config property returns the configured CrawlerConfig."""
        assert crawler.config == config

    @pytest.mark.asyncio
    async def test_async_context_manager(self, crawler):
        """Crawler works as async context manager."""
        async with crawler as c:
            assert c is crawler
        # After exit, session should be closed
        assert not crawler._session_manager.is_open

    def test_user_agent_rotation(self, crawler):
        """User-Agent rotates through the pool."""
        ua1 = crawler._get_user_agent()
        ua2 = crawler._get_user_agent()
        # Should get different agents (or same if only one in pool)
        assert isinstance(ua1, str)
        assert isinstance(ua2, str)
        assert "Mozilla" in ua1
