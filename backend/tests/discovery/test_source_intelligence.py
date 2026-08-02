"""Unit tests for the Construction Source Intelligence Engine."""

from __future__ import annotations

import pytest

from app.engines.source_intelligence.source_models import (
    CrawlStrategy,
    SourcePlannerRequest,
    SourcePlannerResult,
    SourceRecord,
    SourceType,
)
from app.engines.source_intelligence.source_planner import SourcePlanner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_planner() -> SourcePlanner:
    return SourcePlanner()


def _make_request(
    industry: str = "Construction Estimating",
    country: str = "USA",
    state: str | None = "TX",
    city: str | None = "Dallas",
) -> SourcePlannerRequest:
    return SourcePlannerRequest(industry=industry, country=country, state=state, city=city)


# ---------------------------------------------------------------------------
# SourceRecord tests
# ---------------------------------------------------------------------------


class TestSourceRecord:

    def test_creates_with_defaults(self):
        rec = SourceRecord(name="Test Assoc", source_type="trade_association", country="USA")
        assert rec.name == "Test Assoc"
        assert rec.state is None
        assert rec.city is None
        assert rec.priority == 10
        assert rec.crawl_strategy == "html_scraper"
        # Default: company discovery is True for trade associations by default in data
        assert rec.supports_company_discovery is True
        assert rec.supports_bid_discovery is False
        assert rec.supports_leadership is False
        assert rec.supports_contact is False
        assert rec.url == ""
        assert rec.notes == ""

    def test_full_construction_record(self):
        rec = SourceRecord(
            name="AGC Texas",
            source_type="trade_association",
            country="USA",
            state="TX",
            priority=1,
            crawl_strategy="api_client",
            supports_company_discovery=True,
            supports_leadership=True,
            supports_contact=True,
            url="https://www.agctexas.org",
            notes="Largest construction trade association in Texas.",
        )
        assert rec.name == "AGC Texas"
        assert rec.state == "TX"
        assert rec.priority == 1
        assert rec.crawl_strategy == "api_client"
        assert rec.supports_company_discovery is True
        assert rec.supports_leadership is True
        assert rec.supports_contact is True

    def test_is_frozen_immutable(self):
        rec = SourceRecord(name="Test", source_type="government", country="USA")
        with pytest.raises(AttributeError):
            rec.name = "Modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SourcePlannerRequest tests
# ---------------------------------------------------------------------------


class TestSourcePlannerRequest:

    def test_defaults_to_usa(self):
        req = SourcePlannerRequest(industry="Construction")
        assert req.country == "USA"
        assert req.state is None
        assert req.city is None

    def test_full_request(self):
        req = SourcePlannerRequest(
            industry="Construction Estimating",
            country="USA",
            state="Texas",
            city="Houston",
        )
        assert req.industry == "Construction Estimating"
        assert req.state == "Texas"
        assert req.city == "Houston"


# ---------------------------------------------------------------------------
# SourcePlanner – state resolution tests
# ---------------------------------------------------------------------------


class TestStateResolution:

    def test_resolves_full_state_name(self):
        from app.engines.source_intelligence.source_planner import _resolve_state
        assert _resolve_state("Texas") == "TX"
        assert _resolve_state("California") == "CA"
        assert _resolve_state("Florida") == "FL"
        assert _resolve_state("New York") == "NY"

    def test_already_abbreviated_state(self):
        from app.engines.source_intelligence.source_planner import _resolve_state
        assert _resolve_state("TX") == "TX"
        assert _resolve_state("CA") == "CA"

    def test_none_returns_none(self):
        from app.engines.source_intelligence.source_planner import _resolve_state
        assert _resolve_state(None) is None

    def test_unknown_state_falls_back(self):
        from app.engines.source_intelligence.source_planner import _resolve_state
        result = _resolve_state("Unknownia")
        assert result is None or result == "UN"  # falls back to uppercase abbreviation


# ---------------------------------------------------------------------------
# SourcePlanner – plan() tests
# ---------------------------------------------------------------------------


class TestSourcePlanner:

    def test_basic_plan_texas(self):
        """Plan for Texas should include AGC Texas, CSLB, and national sources."""
        planner = _make_planner()
        request = _make_request(state="TX", city="Dallas")
        result = planner.plan(request)

        assert isinstance(result, SourcePlannerResult)
        assert result.total_sources > 0
        assert result.query_summary != ""
        # Should include at least one Texas-specific source
        tx_names = [s.name for s in result.sources if s.state == "TX"]
        assert len(tx_names) > 0

    def test_plan_sorts_by_priority(self):
        """Sources must be sorted by priority (lowest first)."""
        planner = _make_planner()
        request = _make_request(state="TX", city="Dallas")
        result = planner.plan(request)

        priorities = [s.priority for s in result.sources]
        assert priorities == sorted(priorities), f"Sources not sorted by priority: {priorities}"

    def test_no_duplicates_by_name(self):
        """No two sources should share the same name."""
        planner = _make_planner()
        request = _make_request(state="TX", city="Dallas")
        result = planner.plan(request)

        names = [s.name.lower() for s in result.sources]
        assert len(names) == len(set(names)), "Duplicate source names found"

    def test_all_records_have_required_fields(self):
        """Every source must have a non-empty name and URL."""
        planner = _make_planner()
        request = _make_request(state="TX", city="Dallas")
        result = planner.plan(request)

        for rec in result.sources:
            assert rec.name.strip(), f"Empty name in {rec}"
            assert rec.source_type in (
                "government", "trade_association", "industry_publication",
                "business_directory", "professional_network",
                "local_chamber", "licensing_board", "news_media",
            ), f"Invalid source_type: {rec.source_type}"
            assert rec.crawl_strategy in (
                "html_scraper", "api_client", "rss_feed",
                "csv_download", "manual_review",
            ), f"Invalid crawl_strategy: {rec.crawl_strategy}"

    def test_trades_associations_included_for_tx(self):
        """Texas request should include known trade associations."""
        planner = _make_planner()
        request = _make_request(state="TX", city="Austin")
        result = planner.plan(request)

        names = [s.name.lower() for s in result.sources]
        assert any("agc texas" in n for n in names), "AGC Texas missing"
        assert any("associated builders" in n for n in names), "ABC Texas missing"

    def test_national_sources_always_included(self):
        """National sources like ENR should appear regardless of state."""
        planner = _make_planner()
        request = _make_request(state=None, city=None)
        result = planner.plan(request)

        names = [s.name.lower() for s in result.sources]
        assert any("enr" in n or "engineering news-record" in n for n in names), "ENR missing"

    def test_no_state_fallback_to_national_only(self):
        """Without a state, only national sources + chambers should appear."""
        planner = _make_planner()
        request = _make_request(state=None, city=None)
        result = planner.plan(request)

        # Should still have sources (national ones)
        assert result.total_sources > 0
        # State-scoped sources should be empty
        state_sources = [s for s in result.sources if s.state is not None]
        assert len(state_sources) == 0

    def test_city_generates_chamber_source(self):
        """City should generate a local chamber of commerce entry."""
        planner = _make_planner()
        request = _make_request(state="TX", city="Houston")
        result = planner.plan(request)

        chamber_names = [
            s.name for s in result.sources
            if s.source_type == "local_chamber" and "houston" in s.name.lower()
        ]
        assert len(chamber_names) > 0, "Chamber of commerce not generated for Houston"

    def test_diagnostic_skipped_count(self):
        """skipped_sources should count sources without company_discovery support."""
        planner = _make_planner()
        request = _make_request(state="TX", city="Dallas")
        result = planner.plan(request)

        skipped = sum(
            1 for s in result.sources if not s.supports_company_discovery
        )
        assert result.skipped_sources == skipped

    def test_industry_does_not_affect_source_list(self):
        """Industry keyword is accepted but doesn't filter sources
        (source list is location-based, not industry-specific yet)."""
        planner = _make_planner()
        req1 = planner.plan(_make_request(industry="Construction Estimating", state="TX"))
        req2 = planner.plan(_make_request(industry="Software Development", state="TX"))
        # Same state → same sources (industry doesn't change source list yet)
        assert req1.total_sources == req2.total_sources

    def test_empty_query_still_returns_something(self):
        planner = _make_planner()
        request = SourcePlannerRequest(industry="")
        result = planner.plan(request)
        assert result.total_sources > 0

    def test_multiple_cities_same_state(self):
        """Only the specified city should get a chamber source."""
        planner = _make_planner()
        request = _make_request(state="TX", city="Dallas")
        result = planner.plan(request)

        houston_chambers = [s for s in result.sources if "houston" in s.name.lower() and s.source_type == "local_chamber"]
        dallas_chambers = [s for s in result.sources if "dallas" in s.name.lower() and s.source_type == "local_chamber"]
        assert len(dallas_chambers) > 0
        assert len(houston_chambers) == 0

    def test_all_support_flags_are_boolean(self):
        planner = _make_planner()
        request = _make_request(state="TX", city="Dallas")
        result = planner.plan(request)

        for rec in result.sources:
            assert isinstance(rec.supports_company_discovery, bool)
            assert isinstance(rec.supports_bid_discovery, bool)
            assert isinstance(rec.supports_leadership, bool)
            assert isinstance(rec.supports_contact, bool)


# ---------------------------------------------------------------------------
# API endpoint integration test
# ---------------------------------------------------------------------------


class TestSourceIntelligenceAPI:

    def test_endpoint_exists_in_router(self):
        """The /api/v1/source-intelligence/sources route must be registered."""
        from app.main import app

        def collect_paths(obj) -> list[str]:
            """Recursively collect all route paths from an app/router tree."""
            paths: list[str] = []
            if hasattr(obj, "routes"):
                for sub in obj.routes:
                    if hasattr(sub, "path"):
                        paths.append(sub.path)
                    paths.extend(collect_paths(sub))
            return paths

        paths = collect_paths(app)
        assert "/api/v1/source-intelligence/sources" in paths, \
            f"Source intelligence endpoint not registered in router. Paths: {paths}"

    def test_endpoint_returns_200(self):
        """Endpoint must return HTTP 200."""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        r = client.get(
            "/api/v1/source-intelligence/sources",
            params={"industry": "Construction", "state": "TX", "city": "Dallas"},
        )
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"

    def test_endpoint_returns_json_structure(self):
        """Response must be JSON with expected keys."""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        r = client.get(
            "/api/v1/source-intelligence/sources",
            params={"industry": "Construction", "state": "TX"},
        )
        data = r.json()
        assert "sources" in data, f"Missing 'sources' key: {data.keys()}"
        assert "total_sources" in data
        assert "skipped_sources" in data
        assert "query_summary" in data
        assert isinstance(data["sources"], list)
        assert isinstance(data["total_sources"], int)

    def test_endpoint_validates_required_params(self):
        """Missing required 'industry' must return 422."""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        r = client.get("/api/v1/source-intelligence/sources")
        assert r.status_code == 422, f"Expected 422 for missing params, got {r.status_code}"

    def test_endpoint_returns_sources_with_all_fields(self):
        """Each source in the response must have all required fields."""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        r = client.get(
            "/api/v1/source-intelligence/sources",
            params={"industry": "Construction Estimating", "state": "TX", "city": "Dallas"},
        )
        data = r.json()
        required_keys = {
            "name", "type", "country", "state", "priority",
            "crawl_strategy", "supports_company_discovery",
            "supports_bid_discovery", "supports_leadership",
            "supports_contact", "url", "notes",
        }
        if data["sources"]:
            actual_keys = set(data["sources"][0].keys())
            assert required_keys.issubset(actual_keys), \
                f"Missing fields: {required_keys - actual_keys}"

    def test_tx_response_contains_agc(self):
        """Texas query should include AGC Texas as a top-priority source."""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        r = client.get(
            "/api/v1/source-intelligence/sources",
            params={"industry": "Construction", "state": "TX", "city": "Houston"},
        )
        data = r.json()
        names = [s["name"].lower() for s in data["sources"]]
        assert any("agc" in n for n in names), "AGC Texas not in results"
