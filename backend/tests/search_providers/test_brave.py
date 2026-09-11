"""Tests for Brave Search provider."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.search_providers.brave import (
    BraveAPIError,
    BraveAuthError,
    BraveSearchProvider,
)
from app.search_providers.models import SearchQuery


class TestBraveSearchProvider:
    """Test Brave Search provider implementation."""

    @pytest.fixture(autouse=True)
    def _reset_rate_limiter(self):
        """Start every test with a clean rate-limiter clock.

        The limiter is CLASS-level (it must survive admin key hot-swaps and
        pace calls across event loops), so without this reset one test's
        reservation makes the NEXT test sleep up to 1.1s for a slot it never
        needed.
        """
        BraveSearchProvider._rate_next_slot = 0.0
        yield
        BraveSearchProvider._rate_next_slot = 0.0

    @pytest.fixture
    def provider_no_key(self):
        """Provider without API key."""
        return BraveSearchProvider(api_key=None)

    @pytest.fixture
    def provider_with_key(self):
        """Provider with default API key."""
        return BraveSearchProvider(api_key="test-key-123")

    @pytest.fixture
    def provider_custom_max(self):
        """Provider with custom max_results."""
        return BraveSearchProvider(api_key="test-key-123", max_results=10)

    @pytest.fixture
    def mock_session(self):
        """Create a mock aiohttp session."""
        session = MagicMock()
        session.closed = False
        return session

    def test_provider_name(self, provider_with_key):
        """Provider has correct name."""
        assert provider_with_key.provider_name == "brave"

    def test_provider_priority(self, provider_with_key):
        """Brave sorts BEFORE Tavily (priority 15 < 20) — it is the primary
        provider whose free quota is spent first; Tavily is the fallback."""
        assert provider_with_key.priority == 15
        from app.search_providers.tavily import TavilySearchProvider

        assert provider_with_key.priority < TavilySearchProvider.priority

    def test_timeout_covers_rate_queue(self, provider_with_key):
        """The manager backstop (timeout_s) must exceed the aiohttp request
        timeout by enough to absorb the rate-limiter queue wait — a backstop
        that fires while a query is waiting for its 1s slot would blacklist
        a healthy provider."""
        assert provider_with_key.timeout_s > provider_with_key._timeout

    def test_brave_ordered_before_tavily_when_registered_later(self):
        """The admin panel can set the Brave key at ANY time — long after
        Tavily was auto-registered at boot. The registry must still try
        Brave FIRST (priority 15), not Tavily."""
        from app.search_providers.registry import SearchProviderRegistry
        from app.search_providers.tavily import TavilySearchProvider

        registry = SearchProviderRegistry()
        registry.register(TavilySearchProvider(api_key="t-key"))  # boot order
        registry.register(BraveSearchProvider(api_key="b-key"))  # added later

        enabled = registry.get_enabled()
        # get_enabled is what SearchProviderManager iterates — Brave first.
        assert [p.provider_name for p in enabled] == ["brave", "tavily"]

    @pytest.mark.asyncio
    async def test_rate_limit_spaces_concurrent_calls(self):
        """Concurrent calls are spaced ≥ interval apart (free tier 1 qps).

        Uses a SHORT interval so the test stays fast — the reservation
        arithmetic is what matters, not the real 1.1s spacing.
        """
        import time as _time

        interval = 0.05
        provider = BraveSearchProvider(api_key="k")
        old = BraveSearchProvider._RATE_INTERVAL_S
        BraveSearchProvider._RATE_INTERVAL_S = interval
        try:
            start = _time.monotonic()
            await asyncio.gather(
                provider._respect_rate_limit(),
                provider._respect_rate_limit(),
                provider._respect_rate_limit(),
            )
            elapsed = _time.monotonic() - start
        finally:
            BraveSearchProvider._RATE_INTERVAL_S = old
        # 3 callers: first fires immediately, the other two wait for their
        # reserved slots → at least 2 intervals of total spacing.
        assert elapsed >= 2 * interval - 0.01

    @pytest.mark.asyncio
    async def test_rate_limit_no_wait_when_idle(self):
        """A single call with an idle clock fires immediately (no sleep)."""
        import time as _time

        provider = BraveSearchProvider(api_key="k")
        start = _time.monotonic()
        await provider._respect_rate_limit()
        assert _time.monotonic() - start < 0.05

    def test_health_check(self, provider_with_key):
        """Health check returns enabled status."""
        import asyncio

        result = asyncio.run(provider_with_key.health_check())
        assert result["healthy"] is True
        assert result["provider"] == "brave"

    @pytest.mark.asyncio
    async def test_search_no_api_key(self, provider_no_key):
        """Search without API key returns error, not exception."""
        query = SearchQuery(keywords="roofing")
        response = await provider_no_key.search(query)
        assert response.status == "error"
        assert "api key" in response.error.lower()
        assert len(response.results) == 0

    @pytest.mark.asyncio
    async def test_search_success(self, provider_with_key, mock_session):
        """Successful search returns parsed results."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={
                "web": {
                    "results": [
                        {
                            "url": "https://roofing-pro.com",
                            "title": "Pro Roofing Dallas",
                            "description": "Commercial roofing contractor",
                        },
                    ]
                },
                "total": 1,
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get.return_value = mock_response

        with patch.object(
            provider_with_key, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="roofing dallas", num_results=5)
            response = await provider_with_key.search(query)

        assert response.status == "success"
        assert len(response.results) == 1
        assert response.results[0].title == "Pro Roofing Dallas"
        assert response.provider == "brave"

    @pytest.mark.asyncio
    async def test_search_auth_error(self, provider_with_key, mock_session):
        """Authentication error returns proper error response."""
        mock_response = MagicMock()
        mock_response.status = 401
        mock_response.text = AsyncMock(return_value="Unauthorized")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get.return_value = mock_response

        with patch.object(
            provider_with_key, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="test")
            response = await provider_with_key.search(query)

        assert response.status == "error"
        assert "authentication" in response.error.lower() or "401" in response.error

    @pytest.mark.asyncio
    async def test_search_api_error(self, provider_with_key, mock_session):
        """API error returns proper error response."""
        mock_response = MagicMock()
        mock_response.status = 429
        mock_response.text = AsyncMock(return_value="Rate limited")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get.return_value = mock_response

        with patch.object(
            provider_with_key, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="test")
            response = await provider_with_key.search(query)

        assert response.status == "error"
        assert "429" in response.error

    @pytest.mark.asyncio
    async def test_search_generic_exception(self, provider_with_key, mock_session):
        """Generic exception returns error response."""
        mock_session.get.side_effect = ConnectionError("Network down")

        with patch.object(
            provider_with_key, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="test")
            response = await provider_with_key.search(query)

        assert response.status == "error"
        assert "Brave Search error" in response.error

    @pytest.mark.asyncio
    async def test_close_without_session(self, provider_with_key):
        """Close without session does not crash."""
        await provider_with_key.close()

    def test_build_headers(self, provider_with_key):
        """Headers include API key."""
        headers = provider_with_key._build_headers()
        assert "X-Subscription-Token" in headers
        assert headers["X-Subscription-Token"] == "test-key-123"

    def test_build_params_default_max(self, provider_with_key):
        """Params use default max_results when query requests more."""
        query = SearchQuery(keywords="roofing austin", num_results=10)
        params = provider_with_key._build_params(query)
        assert params["q"] == "roofing austin"
        # Default max_results is 20, capped at 50
        assert params["count"] == "20"

    def test_build_params_respects_custom_max(self, provider_custom_max):
        """Params respect custom max_results setting."""
        query = SearchQuery(keywords="test", num_results=10)
        params = provider_custom_max._build_params(query)
        assert params["count"] == "10"

    def test_build_params_with_location(self, provider_with_key):
        """Params include location hints."""
        query = SearchQuery(keywords="plumbing", location="Houston TX")
        params = provider_with_key._build_params(query)
        assert params["search_lang"] == "en"
        assert params["regions"] == "us-en"

    def test_parse_results_empty(self, provider_with_key):
        """Empty results handled gracefully."""
        query = SearchQuery(keywords="test")
        results = provider_with_key._parse_results({"web": {"results": []}}, query)
        assert results == []

    def test_parse_results_with_data(self, provider_with_key):
        """Results parsed correctly from Brave format."""
        query = SearchQuery(keywords="test")
        data = {
            "web": {
                "results": [
                    {
                        "url": "https://a.com",
                        "title": "A Company",
                        "description": "Description A",
                    },
                ]
            },
            "total": 1,
        }
        results = provider_with_key._parse_results(data, query)
        assert len(results) == 1
        assert results[0].title == "A Company"
        assert results[0].snippet == "Description A"

    def test_parse_results_skips_invalid(self, provider_with_key):
        """Entries without URL or title are skipped."""
        query = SearchQuery(keywords="test")
        data = {
            "web": {
                "results": [
                    {"url": "", "title": "No URL", "description": ""},
                    {
                        "url": "https://valid.com",
                        "title": "Valid",
                        "description": "Good",
                    },
                ]
            }
        }
        results = provider_with_key._parse_results(data, query)
        assert len(results) == 1
        assert results[0].url == "https://valid.com"


class TestBraveExceptions:
    """Test Brave exception classes."""

    def test_brave_auth_error(self):
        """Auth error has correct message."""
        exc = BraveAuthError("Unauthorized access")
        assert "Unauthorized" in str(exc)

    def test_brave_api_error_stores_status(self):
        """API error stores status code for inspection."""
        exc = BraveAPIError(status=500, message="Server error")
        assert exc.status == 500
        assert "Server error" in str(exc)
