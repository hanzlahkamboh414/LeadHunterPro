"""Tests for Discovery Sources — FixtureSource and SearchProviderSource."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.discovery.sources.fixture_source import FixtureSource
from app.discovery.sources.search_provider_source import SearchProviderSource
from app.discovery.sources.status import SourceStatus
from app.search_providers.registry import clear_registry, get_registry

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _fixture_env_test_mode(monkeypatch):
    """Accuracy-first Phase 1: FixtureSource is enabled only in demo/test mode.

    These tests exercise the fixture dataset itself, so they run under
    LEADHUNTER_ENV=test where the bridge is permitted. Fixture use in live
    mode is covered by tests/verification/test_phase1_modes_and_tiers.py.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "LEADHUNTER_ENV", "test")


# ---------------------------------------------------------------------------
# Test: FixtureSource
# ---------------------------------------------------------------------------


class TestFixtureSource:
    """Test FixtureSource behaviour."""

    def test_loaded_with_real_fixture(self):
        """FixtureSource loads companies from the real fixture file."""
        source = FixtureSource()
        assert source.source_name == "fixture_bridge"
        assert source.priority == 999
        assert source.enabled is True
        assert len(source._companies) > 0  # 89 companies in fixture

    def test_discover_returns_companies(self):
        """discover() returns matching companies from fixture."""
        source = FixtureSource()
        status, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=5
        )
        assert isinstance(companies, list)
        assert len(companies) <= 5
        assert status == SourceStatus.SUCCESS
        assert meta["data_source"] == "fixture"
        assert meta["temporary"] is True

    def test_discover_state_filter(self):
        """State filter works correctly."""
        source = FixtureSource()
        _, companies, _ = source.discover(
            industry="Roofing", location="Houston Texas", limit=20
        )
        # All returned should be TX
        assert all(c.get("state") == "TX" for c in companies)

    def test_discover_limit_respected(self):
        """Limit parameter truncates output."""
        source = FixtureSource()
        _, companies, _ = source.discover(
            industry="General Contractor", location="TX", limit=3
        )
        assert len(companies) <= 3

    def test_missing_fixture_file(self, tmp_path, monkeypatch):
        """When fixture file is missing, discover returns empty gracefully."""
        bad_path = tmp_path / "nonexistent.json"
        source = FixtureSource(fixture_path=bad_path)
        status, companies, meta = source.discover(industry="Roofing", location="TX", limit=5)
        assert companies == []
        assert status == SourceStatus.EMPTY
        assert meta["data_source"] == "missing"

    def test_health_check(self):
        """health_check reports healthy when fixtures loaded."""
        source = FixtureSource()
        result = asyncio.run(source.health_check())
        assert result["healthy"] is True
        assert result["record_count"] > 0
        assert "EMERGENCY BRIDGE" in result["note"]


# ---------------------------------------------------------------------------
# Test: SearchProviderSource
# ---------------------------------------------------------------------------


class TestSearchProviderSource:
    """Test SearchProviderSource behaviour."""

    def setup_method(self):
        clear_registry()

    def teardown_method(self):
        clear_registry()

    def test_no_providers_returns_empty(self):
        """With no providers registered, returns empty without crashing."""
        source = SearchProviderSource()
        status, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )
        assert companies == []
        assert status.value == "empty"
        assert meta["error"] == "no_providers_configured"

    def test_attributes_set(self):
        """Class attributes match expected values."""
        source = SearchProviderSource()
        assert source.source_name == "search_providers"
        assert source.priority == 50
        assert source.enabled is True

    @pytest.mark.asyncio
    async def test_health_check_no_providers(self):
        """Health check reports unhealthy when no providers."""
        source = SearchProviderSource()
        result = await source.health_check()
        assert result["healthy"] is False
        assert result["providers_registered"] == 0

    def test_with_mock_provider(self):
        """When a provider is registered, discover attempts it."""
        from app.search_providers.models import (
            SearchQuery,
            SearchResponse,
            SearchResult,
        )

        class MockProvider:
            provider_name = "mock_search"
            description = "Mock search provider"
            priority = 10
            enabled = True

            async def search(self, query: SearchQuery) -> SearchResponse:
                return SearchResponse(
                    results=[
                        SearchResult(
                            title="Mock Roofing Co",
                            url="https://mockroofing.com",
                            snippet="Roofing contractor",
                            position=1,
                        ),
                    ],
                    provider="mock_search",
                    query=query.keywords,
                    status="success",
                )

            async def health_check(self) -> dict:
                return {"healthy": True}

        reg = get_registry()
        reg.register(MockProvider())

        source = SearchProviderSource()
        status, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=5
        )
        assert status.value == "success"
        assert len(companies) >= 1
        assert companies[0]["company_name"] == "Mock Roofing Co"
        assert companies[0]["website"] == "https://mockroofing.com"
        assert meta["results_count"] >= 1

    def test_provider_error_returns_empty(self):
        """When provider returns error, discover handles it gracefully."""
        from app.search_providers.models import SearchQuery, SearchResponse

        class FailingProvider:
            provider_name = "fail"
            description = "Failing provider"
            priority = 10
            enabled = True

            async def search(self, query: SearchQuery) -> SearchResponse:
                return SearchResponse(
                    results=[],
                    provider="fail",
                    query=query.keywords,
                    status="error",
                    error="Connection refused",
                )

            async def health_check(self) -> dict:
                return {"healthy": False}

        reg = get_registry()
        reg.register(FailingProvider())

        source = SearchProviderSource()
        status, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=5
        )
        assert companies == []
        # Error string formatted as "{provider}: {message}" by SearchProviderManager
        assert "Connection refused" in meta["error"]

    def test_company_output_shape(self):
        """Output companies have the expected shape for downstream use."""
        from app.search_providers.models import (
            SearchQuery,
            SearchResponse,
            SearchResult,
        )

        class EmptyProvider:
            provider_name = "empty"
            description = "Empty results"
            priority = 10
            enabled = True

            async def search(self, query: SearchQuery) -> SearchResponse:
                return SearchResponse(
                    results=[
                        SearchResult(
                            title="Test Roofing LLC",
                            url="https://testroofing.example.com",
                            snippet="Licensed roofing contractor",
                            position=1,
                        ),
                    ],
                    provider="empty",
                    query=query.keywords,
                    status="success",
                )

            async def health_check(self) -> dict:
                return {"healthy": True}

        reg = get_registry()
        reg.register(EmptyProvider())

        source = SearchProviderSource()
        _, companies, _ = source.discover(
            industry="Roofing", location="Austin Texas", limit=5
        )
        assert len(companies) == 1
        c = companies[0]
        assert "company_name" in c
        assert "website" in c
        # The search query "Austin Texas" is search intent, never company
        # location evidence — the record stays unknown until verified.
        assert c["state"] == ""
        assert c["city"] == ""
        assert c["country"] == "USA"
