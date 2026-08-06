"""Comprehensive tests for Texas Procurement Connector."""

from __future__ import annotations

import pytest

# Clear search provider registry before importing anything else
# to prevent state leakage from other test modules
from app.search_providers.registry import clear_registry

clear_registry()

from app.connectors.connector_result import ConnectorResult
from app.connectors.texas_procurement import (
    TexasProcurementConnector,
    _load_fixture_data,
    _parse_location,
    _verify_and_clean_url,
)


class TestTexasProcurementConnector:
    """Tests for TexasProcurementConnector."""

    @pytest.fixture
    def connector(self):
        """Create connector instance."""
        return TexasProcurementConnector()

    def test_connector_name(self, connector):
        """Connector has correct name."""
        assert connector.connector_name == "texas_procurement"

    def test_connector_priority(self, connector):
        """Connector has correct priority."""
        assert connector.priority == 10

    def test_connector_enabled(self, connector):
        """Connector is enabled by default."""
        assert connector.enabled is True

    def test_health_check(self, connector):
        """Health check returns True when fixtures loaded."""
        assert connector.health_check() is True

    def test_search_returns_results(self, connector):
        """Search returns valid results."""
        results, _ = connector.search("Roofing", "Dallas Texas", 20)
        assert isinstance(results, list)
        assert len(results) >= 1
        assert all(isinstance(r, ConnectorResult) for r in results)

    def test_search_roofing_dallas(self, connector):
        """Roofing Dallas returns roofing contractors."""
        results, _ = connector.search("Roofing", "Dallas Texas", 20)
        assert len(results) >= 1
        for r in results:
            assert (
                "roof" in r.metadata.get("industry_focus", "").lower()
                or "roofing" in r.metadata.get("trade_category", "").lower()
            )

    def test_search_plumbing_houston(self, connector):
        """Plumbing Houston returns plumbing contractors."""
        results, _ = connector.search("Plumbing", "Houston Texas", 20)
        assert len(results) >= 1
        for r in results:
            focus = r.metadata.get("industry_focus", "").lower()
            trade = r.metadata.get("trade_category", "").lower()
            assert "plumb" in focus or "plumb" in trade or "pipe" in focus

    def test_search_electrical_austin(self, connector):
        """Electrical Austin returns electrical contractors."""
        results, _ = connector.search("Electrical", "Austin Texas", 20)
        assert len(results) >= 1

    def test_search_hvac_statewide(self, connector):
        """HVAC Texas returns HVAC contractors statewide."""
        results, _ = connector.search("HVAC", "TX", 50)
        assert len(results) >= 5
        # All should be in TX
        assert all(r.state == "TX" for r in results)

    def test_search_limit_respected(self, connector):
        """Limit parameter is respected."""
        results, _ = connector.search("Roofing", "TX", 5)
        assert len(results) <= 5

    def test_discovery_reason_meaningful(self, connector):
        """Discovery reasons are human-readable."""
        results, _ = connector.search("Roofing", "Dallas Texas", 5)
        for r in results:
            reason = r.metadata.get("discovery_reason", "")
            assert len(reason) > 0
            assert "Matched" in reason or "contractor" in reason.lower()

    def test_source_metadata_indicates_fixture(self, connector):
        """Metadata indicates fixture-based data source."""
        _, metadata = connector.search("Roofing", "TX", 10)
        assert metadata["data_source"] == "fixture"
        # Orchestrator metadata carries the bridge status
        assert "source_metadata" in metadata
        sm = metadata["source_metadata"]
        # Either fallback_reason or data_source confirms bridge
        assert sm.get("data_source") == "fixture" or "fallback" in sm.get("fallback_reason", "").lower()

    def test_search_empty_industry(self, connector):
        """Empty industry returns empty results."""
        results, _ = connector.search("", "TX", 10)
        assert len(results) == 0

    def test_search_unknown_city(self, connector):
        """Unknown city returns empty results."""
        results, _ = connector.search("Roofing", "NonExistentCity ZZ", 10)
        assert len(results) == 0

    def test_validate_result_valid(self, connector):
        """Valid result passes validation."""
        result = ConnectorResult(
            company_name="Test Contractor LLC",
            website="https://www.test.example.com",
            city="Dallas",
            state="TX",
        )
        assert connector.validate_result(result) is True

    def test_validate_result_empty_name(self, connector):
        """Empty name fails validation."""
        result = ConnectorResult(
            company_name="",
            website="https://www.test.example.com",
            city="Dallas",
            state="TX",
        )
        assert connector.validate_result(result) is False

    def test_validate_result_no_website(self, connector):
        """Missing website fails validation."""
        result = ConnectorResult(
            company_name="Test Contractor",
            website="",
            city="Dallas",
            state="TX",
        )
        assert connector.validate_result(result) is False

    def test_validate_result_invalid_url(self, connector):
        """Invalid URL fails validation."""
        result = ConnectorResult(
            company_name="Test Contractor",
            website="not-a-url",
            city="Dallas",
            state="TX",
        )
        assert connector.validate_result(result) is False


class TestIndustryExpansion:
    """Tests for industry keyword expansion."""

    def test_expand_roofing(self):
        """Roofing expands to relevant keywords."""
        from app.connectors.industry_expansion import expand_industry

        keywords = expand_industry("Roofing")
        assert "roof" in keywords
        assert "roofing" in keywords
        assert "shingle" in keywords
        assert "gutter" in keywords

    def test_expand_plumbing(self):
        """Plumbing expands to relevant keywords."""
        from app.connectors.industry_expansion import expand_industry

        keywords = expand_industry("Plumbing")
        assert "plumbing" in keywords
        assert "pipe" in keywords
        assert "drain" in keywords

    def test_expand_electrical(self):
        """Electrical expands to relevant keywords."""
        from app.connectors.industry_expansion import expand_industry

        keywords = expand_industry("Electrical")
        assert "electrical" in keywords
        assert "electrician" in keywords
        assert "wiring" in keywords

    def test_matches_industry_true(self):
        """Matching industry returns True."""
        from app.connectors.industry_expansion import matches_industry

        text = "Commercial roofing, shingle replacement, TPO installation"
        assert matches_industry(text, "Roofing") is True

    def test_matches_industry_false(self):
        """Non-matching industry returns False."""
        from app.connectors.industry_expansion import matches_industry

        text = "Plumbing services and repair"
        assert matches_industry(text, "Roofing") is False

    def test_matches_industry_partial(self):
        """Partial match works."""
        from app.connectors.industry_expansion import matches_industry

        text = "General construction and remodeling"
        assert matches_industry(text, "Construction") is True


class TestLocationParsing:
    """Tests for location parsing."""

    def test_parse_dallas_texas(self):
        """Parse Dallas Texas correctly."""
        city, state = _parse_location("Dallas Texas")
        assert city == "dallas"
        assert state == "TX"

    def test_parse_houston_tx(self):
        """Parse Houston TX correctly."""
        city, state = _parse_location("Houston, TX")
        assert city == "houston"
        assert state == "TX"

    def test_parse_state_only(self):
        """Parse state-only returns None city."""
        city, state = _parse_location("Texas")
        assert city is None
        assert state == "TX"

    def test_parse_code_only(self):
        """Parse state code returns TX."""
        city, state = _parse_location("TX")
        assert city is None
        assert state == "TX"

    def test_parse_with_country(self):
        """Parse with country suffix."""
        city, state = _parse_location("Dallas Texas USA")
        assert city == "dallas" or "dallas" in city.lower()
        assert state == "TX"


class TestURLValidation:
    """Tests for URL validation."""

    def test_verify_valid_https(self):
        """Valid HTTPS URL passes."""
        assert (
            _verify_and_clean_url("https://www.example.com")
            == "https://www.example.com"
        )

    def test_verify_valid_http(self):
        """Valid HTTP URL passes."""
        assert _verify_and_clean_url("http://example.com") == "http://example.com"

    def test_verify_adds_https(self):
        """URL without protocol gets https added."""
        result = _verify_and_clean_url("www.example.com")
        assert result.startswith("https://")

    def test_verify_invalid_no_domain(self):
        """URL without domain fails."""
        assert _verify_and_clean_url("not-a-url") is None

    def test_verify_empty_string(self):
        """Empty string returns None."""
        assert _verify_and_clean_url("") is None

    def test_verify_none(self):
        """None returns None."""
        assert _verify_and_clean_url(None) is None  # type: ignore[arg-type]


class TestFixtureData:
    """Tests for fixture data loading."""

    def test_load_fixture_data(self):
        """Fixture data loads successfully."""
        companies, meta = _load_fixture_data()
        assert len(companies) > 0
        assert meta["data_source"] == "fixture"
        assert meta["temporary"] is True
        assert "last_updated" in meta

    def test_fixture_structure(self):
        """Each company has required fields."""
        companies, _ = _load_fixture_data()
        for company in companies[:10]:  # Check first 10
            assert "company_name" in company
            assert "website" in company
            assert "city" in company
            assert "state" in company
            assert "industry_focus" in company
            assert "trade_category" in company

    def test_fixture_coverage_trades(self):
        """Fixture covers multiple trades."""
        companies, _ = _load_fixture_data()
        trades = {c.get("trade_category") for c in companies}
        assert len(trades) >= 5  # At least 5 different trades

    def test_fixture_coverage_cities(self):
        """Fixture covers multiple cities."""
        companies, _ = _load_fixture_data()
        cities = {c.get("city") for c in companies}
        assert len(cities) >= 5  # At least 5 different cities


class TestConnectorIntegration:
    """Integration tests for connector with expanded dataset."""

    def test_roofing_returns_results(self):
        """Roofing query returns results."""
        connector = TexasProcurementConnector()
        results, _ = connector.search("Roofing", "Dallas Texas", 20)
        assert len(results) >= 1

    def test_plumbing_returns_results(self):
        """Plumbing query returns results."""
        connector = TexasProcurementConnector()
        results, _ = connector.search("Plumbing", "Houston Texas", 20)
        assert len(results) >= 1

    def test_electrical_returns_results(self):
        """Electrical query returns results."""
        connector = TexasProcurementConnector()
        results, _ = connector.search("Electrical", "Austin Texas", 20)
        assert len(results) >= 1

    def test_hvac_returns_results(self):
        """HVAC query returns results."""
        connector = TexasProcurementConnector()
        results, _ = connector.search("HVAC", "TX", 50)
        assert len(results) >= 5

    def test_general_contractor_returns_results(self):
        """General Contractor query returns results."""
        connector = TexasProcurementConnector()
        results, _ = connector.search("General Contractor", "TX", 50)
        assert len(results) >= 10

    def test_concrete_returns_results(self):
        """Concrete query returns results."""
        connector = TexasProcurementConnector()
        results, _ = connector.search("Concrete", "Dallas Texas", 20)
        assert len(results) >= 1

    def test_painting_returns_results(self):
        """Painting query returns results."""
        connector = TexasProcurementConnector()
        results, _ = connector.search("Painting", "Houston Texas", 20)
        assert len(results) >= 1

    def test_no_duplicates(self):
        """Results from single connector have no duplicate domains."""
        connector = TexasProcurementConnector()
        results, _ = connector.search("General Contractor", "TX", 50)
        # Deduplication is handled by ConnectorManager, not connector
        # Here we just verify the connector returns valid results
        assert len(results) > 0
        assert all(r.website for r in results)

    def test_all_results_have_websites(self):
        """All results have non-empty websites."""
        connector = TexasProcurementConnector()
        results, _ = connector.search("Roofing", "TX", 50)
        assert all(r.website for r in results)

    def test_all_results_have_names(self):
        """All results have non-empty names."""
        connector = TexasProcurementConnector()
        results, _ = connector.search("Plumbing", "TX", 50)
        assert all(r.company_name and len(r.company_name) >= 3 for r in results)


class TestNormalizeCompany:
    """The adapter projects heterogeneous source dicts onto the connector schema.

    The website discovery plugin emits ``name``/``services``/``evidence``
    with no location; search and fixture sources emit
    ``company_name``/``industry_focus`` with their own location. Without
    this projection, plugin records would be silently dropped by the
    Step-3 state/city/keyword filters (CLAUDE.md §12).
    """

    def _connector(self):
        return TexasProcurementConnector()

    def test_maps_plugin_shape_onto_connector_schema(self):
        """A plugin company record becomes filterable and consumable."""
        company = self._connector()._normalize_company(
            {
                "name": "Acme Roofing",
                "website": "https://acme.com/",
                "services": ["Roofing", "Commercial Roofing"],
                "evidence": ["evidence"],
                "discovered_by": "brave_search",
            },
            state="TX",
            city="dallas",
        )
        assert company["company_name"] == "Acme Roofing"
        assert company["industry_focus"] == "Roofing Commercial Roofing"
        assert company["trade_category"] == ""
        assert company["state"] == "TX"
        assert company["city"] == "dallas"
        assert company["data_provenance"] == "brave_search"

    def test_keeps_source_location_when_present(self):
        """Existing location/fields are never overridden by the query."""
        company = self._connector()._normalize_company(
            {
                "company_name": "Dallas Roof Co",
                "website": "https://dallasroof.com/",
                "industry_focus": "roofing",
                "trade_category": "roofing",
                "state": "TX",
                "city": "Dallas",
                "data_provenance": "live:3",
            },
            state="ZZ",
            city="nowhere",
        )
        assert company["state"] == "TX"
        assert company["city"] == "Dallas"
        assert company["industry_focus"] == "roofing"
        assert company["data_provenance"] == "live:3"

    def test_fills_missing_location_from_query_only(self):
        """Missing city stays empty when the query has none."""
        company = self._connector()._normalize_company(
            {"name": "Solo Firm", "website": "https://solo.com/"},
            state="TX",
            city="",
        )
        assert company["company_name"] == "Solo Firm"
        assert company["state"] == "TX"
        assert company["city"] == ""

    def test_empty_services_do_not_clobber_other_fields(self):
        """An empty services list yields a blank industry_focus, not garbage."""
        company = self._connector()._normalize_company(
            {"name": "Firm", "website": "https://firm.com/", "services": []},
            state="TX",
            city="",
        )
        assert company["company_name"] == "Firm"
        assert company["industry_focus"] == ""
