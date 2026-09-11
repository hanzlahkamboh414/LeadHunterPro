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


class _CaptureProvider:
    """Inc 2 test double: a registered provider that records every query it
    receives and returns one result per query."""

    provider_name = "capture"
    description = "Capture"
    priority = 10
    enabled = True
    queries: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.queries = []

    async def search(self, query) -> object:
        import app.search_providers.models as m

        _CaptureProvider.queries.append(query.keywords)
        return m.SearchResponse(
            results=[
                m.SearchResult(
                    title=f"Hit {len(_CaptureProvider.queries)}",
                    url=f"https://hit{len(_CaptureProvider.queries)}.com",
                    snippet="Contractor", position=1,
                ),
            ],
            provider="capture", query=query.keywords, status="success",
        )

    async def health_check(self) -> dict:
        return {"healthy": True}


class _FakeCandStore:
    """Inc 2 test double: a fixed effective web-angle set per layer."""

    def __init__(self, web_angles: list[str]) -> None:
        self._web = web_angles

    def effective_dorks(self, yield_store=None, segment="", layer="dork"):
        return list(self._web) if layer == "web" else []


class _FakeYieldStore:
    """Inc 2 test double: the presence of a yield store is all dispatch needs."""


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

    def test_query_text_never_repeats_trade_word(self):
        """'general contractor' must NOT become 'general contractor contractor
        Dallas Texas' (Step 0 dedup). A repeated token makes the provider query
        noisier AND contaminates query-yield learning with a bad-query signal."""
        from app.search_providers.models import SearchQuery, SearchResponse

        seen: dict[str, str] = {}

        class CaptureProvider:
            provider_name = "capture"
            description = "Capture"
            priority = 10
            enabled = True

            async def search(self, query: SearchQuery) -> SearchResponse:
                seen["keywords"] = query.keywords
                return SearchResponse(results=[], status="success")

            async def health_check(self) -> dict:
                return {"healthy": True}

        reg = get_registry()
        reg.register(CaptureProvider())

        SearchProviderSource().discover(
            industry="general contractor", location="Dallas Texas", limit=5
        )
        # _parse_location lowercases the city and abbreviates the state.
        assert seen["keywords"] == "general contractor dallas TX"

    def test_query_text_appends_contractor_only_when_missing(self):
        """A trade that does NOT name 'contractor' still gets the role word —
        the dedup fix must never STARVE a query of its trade descriptor."""
        from app.search_providers.models import SearchQuery, SearchResponse

        seen: dict[str, str] = {}

        class CaptureProvider:
            provider_name = "capture"
            description = "Capture"
            priority = 10
            enabled = True

            async def search(self, query: SearchQuery) -> SearchResponse:
                seen["keywords"] = query.keywords
                return SearchResponse(results=[], status="success")

            async def health_check(self) -> dict:
                return {"healthy": True}

        reg = get_registry()
        reg.register(CaptureProvider())

        SearchProviderSource().discover(
            industry="Roofing", location="Dallas Texas", limit=5
        )
        assert seen["keywords"] == "Roofing contractor dallas TX"
        # Plural trades (variants pass) are covered by the substring check too.
        reg.unregister("capture")
        reg.register(CaptureProvider())
        SearchProviderSource().discover(
            industry="general contractors", location="Austin Texas", limit=5
        )
        assert seen["keywords"] == "general contractors austin TX"

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
        # Step 0.5 attribution: the producing provider rides a SEPARATE field,
        # while the provenance string keeps its stable "live:{position}" contract
        # so no consumer of the old format breaks (P-H guardrail).
        assert c["provider_name"] == "empty"
        assert c["data_provenance"] == "live:1"

    # ------------------------------------------------------------------
    # Inc 2 — web-angle lane (LLM-invented discovery methods)
    # ------------------------------------------------------------------

    def _with_capture_provider(self):
        _CaptureProvider.reset()
        # The rotation cursor is CLASS-level (survives per-pass re-creation);
        # each test asserts an exact rotation sequence so it starts at 0.
        SearchProviderSource._angle_cursor = 0
        reg = get_registry()
        reg.register(_CaptureProvider())

    def test_web_angle_off_without_stores(self):
        """No injected stores -> exact pre-Inc-2 behavior: ONE query, records
        unstamped (default lane untouched)."""
        from app.search_providers.models import SearchQuery, SearchResponse

        class _Provider:
            provider_name = "p"
            description = "p"
            priority = 10
            enabled = True
            n = {"i": 0}

            async def search(self, query: SearchQuery) -> SearchResponse:
                _Provider.n["i"] += 1
                return SearchResponse(results=[], status="success")

            async def health_check(self) -> dict:
                return {"healthy": True}

        reg = get_registry()
        reg.register(_Provider())
        source = SearchProviderSource()  # no candidate/yield stores
        source.discover(industry="Roofing", location="Dallas Texas", limit=5)
        assert _Provider.n["i"] == 1  # default query only, no angle query

    def test_web_angle_dispatches_and_stamps(self):
        """With stores, each discover() runs the default query PLUS one web
        angle; angle records carry the angle in _discovery_dork (Phase G
        attribution) and the metadata names the angle honestly."""
        self._with_capture_provider()
        angle = "chamber of commerce member directory {industry} {location}"
        source = SearchProviderSource(
            candidate_store=_FakeCandStore([angle]),
            yield_store=_FakeYieldStore(),
        )
        status, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=5,
        )
        assert status.value == "success"
        # TWO queries: default + the angle (placeholders filled).
        assert len(_CaptureProvider.queries) == 2
        assert _CaptureProvider.queries[1] == (
            "chamber of commerce member directory Roofing Dallas Texas"
        )
        stamped = [c for c in companies if c["_discovery_dork"] == angle]
        assert stamped  # angle records attributed for the yield loop
        assert meta["web_angle"] == angle
        assert meta["web_angle_results"] == len(stamped)
        # Default-lane records stay unstamped — attribution is per-record.
        assert any(c["_discovery_dork"] == "" for c in companies)

    def test_web_angle_rotates(self):
        """One angle per discover() call, round-robin: consecutive calls probe
        DIFFERENT angles (bounded credit burn, broad coverage over a sweep)."""
        self._with_capture_provider()
        angles = [
            "license roster {industry} {location}",
            "bid board {industry} {location}",
        ]
        source = SearchProviderSource(
            candidate_store=_FakeCandStore(angles),
            yield_store=_FakeYieldStore(),
        )
        source.discover(industry="Roofing", location="Dallas Texas", limit=5)
        source.discover(industry="Roofing", location="Dallas Texas", limit=5)
        source.discover(industry="Roofing", location="Dallas Texas", limit=5)
        # Each call: 1 default + 1 angle query.
        assert len(_CaptureProvider.queries) == 6
        angle_queries = [q for q in _CaptureProvider.queries
                         if "Roofing" in q and q.split()[0] in
                         {"license", "bid"}]
        assert angle_queries == [
            "license roster Roofing Dallas Texas",
            "bid board Roofing Dallas Texas",
            "license roster Roofing Dallas Texas",  # wraps around
        ]

    def test_web_angle_rotates_across_fresh_instances(self):
        """Inc 2 regression (live proof 2026-09-11): the pipeline re-creates
        SearchProviderSource for EVERY discovery pass — an instance-level
        cursor reset each time and dispatched angle[0] six passes running. The
        rotation cursor must be CLASS-level so a fresh instance still advances."""
        self._with_capture_provider()
        angles = [
            "license roster {industry} {location}",
            "bid board {industry} {location}",
        ]
        # A FRESH source per call, exactly how _build_discovery_orchestrator
        # builds one per run_discovery pass.
        for _ in range(4):
            source = SearchProviderSource(
                candidate_store=_FakeCandStore(angles),
                yield_store=_FakeYieldStore(),
            )
            source.discover(industry="Roofing", location="Dallas Texas", limit=5)
        angle_queries = [q for q in _CaptureProvider.queries
                         if q.split()[0] in {"license", "bid"}]
        assert angle_queries == [
            "license roster Roofing Dallas Texas",
            "bid board Roofing Dallas Texas",
            "license roster Roofing Dallas Texas",  # wraps
            "bid board Roofing Dallas Texas",
        ]

    def test_web_angle_dead_not_dispatched(self):
        """The earn-or-die gate applies to angles: an empty effective web set
        (all proven dead) means NO angle query — the default lane runs alone."""
        self._with_capture_provider()
        source = SearchProviderSource(
            candidate_store=_FakeCandStore([]),
            yield_store=_FakeYieldStore(),
        )
        source.discover(industry="Roofing", location="Dallas Texas", limit=5)
        assert len(_CaptureProvider.queries) == 1  # default only

    def test_web_angle_failure_never_breaks_default(self):
        """An angle query that raises is swallowed (best-effort lane): the
        default query's results still come back, no exception escapes."""
        from app.search_providers.models import SearchQuery, SearchResponse

        class _ExplodeOnAngle:
            provider_name = "boom"
            description = "boom"
            priority = 10
            enabled = True

            async def search(self, query: SearchQuery) -> SearchResponse:
                if "directory" in query.keywords:  # the angle query
                    raise RuntimeError("angle search exploded")
                return SearchResponse(results=[], status="success")

            async def health_check(self) -> dict:
                return {"healthy": True}

        reg = get_registry()
        reg.register(_ExplodeOnAngle())
        source = SearchProviderSource(
            candidate_store=_FakeCandStore(
                ["member directory {industry} {location}"]),
            yield_store=_FakeYieldStore(),
        )
        status, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=5,
        )
        assert status.value == "empty"  # both lanes found nothing — honest
        assert companies == []
        assert "web_angle" not in meta  # no angle claim without angle results
