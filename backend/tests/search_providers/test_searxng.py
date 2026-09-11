"""Tests for SearXNG search provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.search_providers.models import SearchQuery
from app.search_providers.searxng import SearXNGProvider


class TestSearXNGProvider:
    """Test SearXNG provider implementation."""

    @pytest.fixture(autouse=True)
    def _reset_rate_limiter(self):
        """Start every test with a clean rate-limiter clock.

        The limiter is CLASS-level (it must pace calls across the pipeline's
        multiple concurrent event loops), so without this reset one test's
        reservation makes the NEXT test sleep up to 0.3s for a slot it never
        needed.
        """
        SearXNGProvider._rate_next_slot = 0.0
        yield
        SearXNGProvider._rate_next_slot = 0.0

    @pytest.fixture
    def provider(self):
        """Create a SearXNG provider instance."""
        return SearXNGProvider(
            base_url="https://searxng.example.com",
            timeout=5,
            max_results=10,
        )

    @pytest.fixture
    def mock_session(self):
        """Create a mock aiohttp session."""
        session = MagicMock()
        session.closed = False
        return session

    def test_provider_name(self, provider):
        """Provider has correct name."""
        assert provider.provider_name == "searxng"

    def test_provider_priority(self, provider):
        """Provider has correct priority."""
        assert provider.priority == 10

    def test_timeout_defaults_to_fast_cap(self):
        """The per-query aiohttp cap stays short (6s) — a hung instance
        stalls a query at most once — while timeout_s (the manager's hard
        gate) additionally carries a fixed budget for the rate-limiter
        queue wait so a queued query is not cancelled-and-blacklisted."""
        p = SearXNGProvider(base_url="https://searxng.example.com")
        assert p._timeout == 6
        assert p.timeout_s == 6.0 + 15.0
        p2 = SearXNGProvider(base_url="https://searxng.example.com", timeout=3)
        assert p2.timeout_s == 3.0 + 15.0

    def test_timeout_covers_rate_queue(self, provider):
        """The manager backstop (timeout_s) must exceed the aiohttp request
        timeout by enough to absorb the rate-limiter queue wait — a backstop
        that fires while a query is waiting for its slot would blacklist a
        healthy provider (same invariant as brave.py)."""
        assert provider.timeout_s > provider._timeout

    @pytest.mark.asyncio
    async def test_rate_limit_spaces_concurrent_calls(self):
        """Concurrent calls are spaced ≥ interval apart (~3 qps cap).

        Uses a SHORT interval so the test stays fast — the reservation
        arithmetic is what matters, not the real 0.3s spacing.
        """
        import asyncio
        import time as _time

        interval = 0.05
        provider = SearXNGProvider(base_url="https://searxng.example.com")
        old = SearXNGProvider._RATE_INTERVAL_S
        SearXNGProvider._RATE_INTERVAL_S = interval
        try:
            start = _time.monotonic()
            await asyncio.gather(
                provider._respect_rate_limit(),
                provider._respect_rate_limit(),
                provider._respect_rate_limit(),
            )
            elapsed = _time.monotonic() - start
        finally:
            SearXNGProvider._RATE_INTERVAL_S = old
        # 3 callers: first fires immediately, the other two wait for their
        # reserved slots → at least 2 intervals of total spacing.
        assert elapsed >= 2 * interval - 0.01

    @pytest.mark.asyncio
    async def test_rate_limit_no_wait_when_idle(self):
        """A single call with an idle clock fires immediately (no sleep)."""
        import time as _time

        provider = SearXNGProvider(base_url="https://searxng.example.com")
        start = _time.monotonic()
        await provider._respect_rate_limit()
        assert _time.monotonic() - start < 0.05

    def test_health_check(self, provider):
        """Health check returns enabled status."""
        import asyncio

        result = asyncio.run(provider.health_check())
        assert result["healthy"] is True
        assert result["provider"] == "searxng"

    @pytest.mark.asyncio
    async def test_search_success(self, provider, mock_session):
        """Successful search returns parsed results."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={
                "results": [
                    {
                        "url": "https://roofing-company.com",
                        "title": "Dallas Roofing Co",
                        "content": "Best roofing in Dallas",
                        "engine": "google",
                    },
                    {
                        "url": "https://texasroofers.com",
                        "title": "Texas Roofers",
                        "content": "Roofing services",
                        "engine": "bing",
                    },
                ],
                "number_of_results": 150,
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get.return_value = mock_response

        with patch.object(
            provider, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="roofing dallas", num_results=10)
            response = await provider.search(query)

        assert response.status == "success"
        assert len(response.results) == 2
        assert response.provider == "searxng"
        assert response.total_estimated == 150
        assert response.error == ""
        # latency_ms is computed from monotonic clock; just verify it's non-negative
        assert response.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_search_http_error(self, provider, mock_session):
        """HTTP error returns error response."""
        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.text = AsyncMock(return_value="Internal Server Error")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get.return_value = mock_response

        with patch.object(
            provider, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="nonexistent")
            response = await provider.search(query)

        assert response.status == "error"
        assert "HTTP 500" in response.error
        assert len(response.results) == 0

    @pytest.mark.asyncio
    async def test_search_generic_exception(self, provider, mock_session):
        """Generic exception returns error response."""
        mock_session.get.side_effect = ConnectionError("DNS failed")

        with patch.object(
            provider, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="test")
            response = await provider.search(query)

        assert response.status == "error"
        assert "SearXNG error" in response.error

    @pytest.mark.asyncio
    async def test_search_with_location(self, provider, mock_session):
        """Search includes location in query."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"results": [], "number_of_results": 0}
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get.return_value = mock_response

        captured_url = []

        def capture_get(url, **kwargs):
            captured_url.append(url)
            return mock_response

        mock_session.get.side_effect = capture_get

        with patch.object(
            provider, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="plumbing", location="Austin TX")
            await provider.search(query)

        assert len(captured_url) == 1
        assert "Austin" in captured_url[0]

    @pytest.mark.asyncio
    async def test_close_without_session(self, provider):
        """Close without session does not crash."""
        await provider.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_close_twice(self, provider, mock_session):
        """Closing twice is safe."""
        provider._session = mock_session
        await provider.close()
        await provider.close()  # Should not raise

    def test_build_url_basic(self, provider):
        """URL is built correctly for basic query."""
        query = SearchQuery(keywords="roofing", num_results=5)
        url = provider._build_url(query)
        assert "searxng.example.com" in url
        assert "format=json" in url
        assert "roofing" in url

    def test_build_url_with_location(self, provider):
        """URL includes location when provided."""
        query = SearchQuery(keywords="plumbing", location="Houston TX")
        url = provider._build_url(query)
        assert "Houston" in url

    def test_parse_results_empty(self, provider):
        """Empty results handled gracefully."""
        query = SearchQuery(keywords="test")
        results = provider._parse_results({"results": []}, query)
        assert results == []

    def test_parse_results_with_data(self, provider):
        """Results parsed correctly from SearXNG format."""
        query = SearchQuery(keywords="test")
        data = {
            "results": [
                {
                    "url": "https://a.com",
                    "title": "A",
                    "content": "Desc A",
                    "engine": "g",
                },
                {
                    "url": "https://b.com",
                    "title": "B",
                    "content": "Desc B",
                    "engine": "b",
                },
            ],
            "number_of_results": 2,
        }
        results = provider._parse_results(data, query)
        assert len(results) == 2
        assert results[0].title == "A"
        assert results[0].url == "https://a.com"
        assert results[1].snippet == "Desc B"

    def test_parse_results_skips_invalid(self, provider):
        """Entries without URL or title are skipped."""
        query = SearchQuery(keywords="test")
        data = {
            "results": [
                {"url": "", "title": "No URL", "content": ""},  # skipped
                {"url": "https://valid.com", "title": "Valid", "content": "OK"},  # kept
                {"url": "https://no-title.com", "title": "", "content": ""},  # skipped
            ],
        }
        results = provider._parse_results(data, query)
        assert len(results) == 1
        assert results[0].url == "https://valid.com"
