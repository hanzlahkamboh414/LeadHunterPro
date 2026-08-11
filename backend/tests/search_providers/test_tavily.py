"""Tests for Tavily search provider (Inc10 feeder)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.search_providers.models import SearchQuery
from app.search_providers.tavily import TavilySearchProvider


class TestTavilyProvider:
    """Test Tavily provider implementation."""

    @pytest.fixture
    def provider(self):
        """Create a Tavily provider instance with a fake key."""
        return TavilySearchProvider(
            api_key="tvly-test",
            timeout=5,
            max_results=20,
            endpoint="https://api.tavily.example/search",
        )

    @pytest.fixture
    def mock_session(self):
        """Create a mock aiohttp session."""
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        return session

    def test_provider_name(self, provider):
        """Provider has correct name."""
        assert provider.provider_name == "tavily"

    def test_provider_priority(self, provider):
        """Provider has correct priority (REST-tier, peer of Brave)."""
        assert provider.priority == 20

    def test_health_check(self, provider):
        """Health check returns enabled status."""
        import asyncio

        result = asyncio.run(provider.health_check())
        assert result["healthy"] is True
        assert result["provider"] == "tavily"

    @pytest.mark.asyncio
    async def test_search_success(self, provider, mock_session):
        """Successful search returns parsed results."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={
                "query": "roofing dallas",
                "results": [
                    {
                        "title": "Lone Star Roofing LLC",
                        "url": "https://www.lonestarroofing.com/",
                        "content": "Commercial roofing in Dallas, TX.",
                        "score": 0.9,
                    },
                    {
                        "title": "Texas Premier Roofing",
                        "url": "https://texaspremierroofing.com",
                        "content": "Residential roofing services.",
                        "score": 0.8,
                    },
                ],
                "response_time": 0.4,
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post.return_value = mock_response

        with patch.object(
            provider, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="roofing dallas", num_results=10)
            response = await provider.search(query)

        assert response.status == "success"
        assert len(response.results) == 2
        assert response.provider == "tavily"
        assert response.results[0].title == "Lone Star Roofing LLC"
        assert response.results[0].url == "https://www.lonestarroofing.com/"
        assert response.results[1].snippet == "Residential roofing services."
        assert response.error == ""
        # Actual payload is sent to the endpoint (never the fake key in a URL).
        _, kwargs = mock_session.post.call_args
        assert kwargs["json"]["api_key"] == "tvly-test"
        assert kwargs["json"]["max_results"] == 10

    @pytest.mark.asyncio
    async def test_search_auth_error(self, provider, mock_session):
        """401 returns an honest auth error response."""
        mock_response = MagicMock()
        mock_response.status = 401
        mock_response.text = AsyncMock(return_value="invalid api key")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post.return_value = mock_response

        with patch.object(
            provider, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="roofing")
            response = await provider.search(query)

        assert response.status == "error"
        assert "authentication failed" in response.error
        assert len(response.results) == 0

    @pytest.mark.asyncio
    async def test_search_http_error(self, provider, mock_session):
        """HTTP error returns error response."""
        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.text = AsyncMock(return_value="Internal Server Error")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post.return_value = mock_response

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
        mock_session.post.side_effect = ConnectionError("DNS failed")

        with patch.object(
            provider, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="test")
            response = await provider.search(query)

        assert response.status == "error"
        assert "Tavily error" in response.error

    @pytest.mark.asyncio
    async def test_search_requires_key(self, monkeypatch):
        """No key configured -> honest error, never a fabricated request."""
        monkeypatch.setenv("TAVILY_SEARCH_API_KEY", "")
        p = TavilySearchProvider(endpoint="https://api.tavily.example/search")
        assert p._api_key in ("", None)

        query = SearchQuery(keywords="roofing")
        response = await p.search(query)

        assert response.status == "error"
        assert "TAVILY_SEARCH_API_KEY" in response.error
        assert len(response.results) == 0

    @pytest.mark.asyncio
    async def test_search_with_location_appended(self, provider, mock_session):
        """Location is appended to the query keywords."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"results": [], "response_time": 0.1}
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post.return_value = mock_response

        with patch.object(
            provider, "_get_session", new=AsyncMock(return_value=mock_session)
        ):
            query = SearchQuery(keywords="plumbing", location="Austin TX")
            await provider.search(query)

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"]["query"] == "plumbing Austin TX"

    @pytest.mark.asyncio
    async def test_close_without_session(self, provider):
        """Close without session does not crash."""
        await provider.close()

    @pytest.mark.asyncio
    async def test_close_twice(self, provider, mock_session):
        """Closing twice is safe."""
        provider._session = mock_session
        await provider.close()
        await provider.close()

    def test_build_payload_basic(self, provider):
        """Payload is built correctly for a basic query."""
        query = SearchQuery(keywords="roofing", num_results=5)
        payload = provider._build_payload(query)
        assert payload["query"] == "roofing"
        assert payload["max_results"] == 5
        assert payload["search_depth"] == "basic"
        assert payload["api_key"] == "tvly-test"

    def test_build_payload_caps_max_results(self, provider):
        """Payload never exceeds Tavily's 20-result cap."""
        query = SearchQuery(keywords="roofing", num_results=100)
        payload = provider._build_payload(query)
        assert payload["max_results"] == 20

    def test_parse_results_empty(self, provider):
        """Empty results handled gracefully."""
        query = SearchQuery(keywords="test")
        results = provider._parse_results({"results": []}, query)
        assert results == []

    def test_parse_results_with_data(self, provider):
        """Results parsed correctly from Tavily format."""
        query = SearchQuery(keywords="test")
        data = {
            "results": [
                {"url": "https://a.com", "title": "A", "content": "Desc A"},
                {"url": "https://b.com", "title": "B", "content": "Desc B"},
            ]
        }
        results = provider._parse_results(data, query)
        assert len(results) == 2
        assert results[0].title == "A"
        assert results[1].snippet == "Desc B"

    def test_parse_results_skips_invalid(self, provider):
        """Entries without URL or title are skipped."""
        query = SearchQuery(keywords="test")
        data = {
            "results": [
                {"url": "", "title": "No URL", "content": ""},
                {"url": "https://valid.com", "title": "Valid", "content": "OK"},
                {"url": "https://no-title.com", "title": "", "content": ""},
            ]
        }
        results = provider._parse_results(data, query)
        assert len(results) == 1
        assert results[0].url == "https://valid.com"