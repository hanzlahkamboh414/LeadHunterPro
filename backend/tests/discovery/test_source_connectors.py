"""Unit tests for Construction Source Connectors."""

from __future__ import annotations

import pytest

from app.engines.source_connectors.base import ConstructionSourceConnector
from app.engines.source_connectors import (
    ConstructionSourceRegistry,
    list_connectors,
    get_connector,
)
from app.engines.source_connectors.texas_procurement import (
    TexasProcurementConnector,
    _TAXAS_CONSTRUCTION_COMPANIES,
)

# ---------------------------------------------------------------------------
# Base connector interface tests
# ---------------------------------------------------------------------------


class TestConstructionSourceConnector:
    """Tests for the abstract base connector class."""

    def test_is_available_by_default(self):
        """Default implementation returns True."""
        c = TexasProcurementConnector()
        assert c.is_available() is True


# ---------------------------------------------------------------------------
# TexasProcurementConnector tests
# ---------------------------------------------------------------------------


class TestTexasProcurementConnector:

    def test_connector_name(self):
        c = TexasProcurementConnector()
        assert c.name == "texas_procurement"

    def test_connector_description(self):
        c = TexasProcurementConnector()
        assert "texas" in c.description.lower()
        assert "construction" in c.description.lower()

    def test_connector_is_available(self):
        c = TexasProcurementConnector()
        assert c.is_available() is True

    def test_dataset_size(self):
        """Dataset should have 40+ construction companies."""
        assert len(_TAXAS_CONSTRUCTION_COMPANIES) >= 40

    def test_all_records_have_required_fields(self):
        """Every record must have company_name, website, city, state, country, source_url."""
        required = {"company_name", "website", "city", "state", "country", "source_url"}
        for rec in _TAXAS_CONSTRUCTION_COMPANIES:
            missing = required - set(rec.keys())
            assert not missing, f"{rec['company_name']} missing fields: {missing}"

    def test_all_records_have_us_state(self):
        """All records should be in US states."""
        for rec in _TAXAS_CONSTRUCTION_COMPANIES:
            assert rec["country"] == "USA"
            assert rec["state"] in ("TX", "CA", "FL", "NY")

    def test_discover_returns_list_and_dict(self):
        """discover() must return (list, dict) tuple."""
        c = TexasProcurementConnector()
        results, metadata = c.discover(state="TX", limit=10)
        assert isinstance(results, list)
        assert isinstance(metadata, dict)

    def test_discover_no_filters(self):
        """Without filters, should return all TX companies (up to limit)."""
        c = TexasProcurementConnector()
        results, metadata = c.discover(limit=100)
        assert metadata["total_records"] >= 40
        assert metadata["filtered_count"] == len(results)

    def test_discover_state_filter(self):
        """State filter should work correctly."""
        c = TexasProcurementConnector()
        results, metadata = c.discover(state="TX", limit=100)
        assert all(r["state"] == "TX" for r in results)
        assert metadata["filtered_count"] > 0

    def test_discover_city_filter(self):
        """City filter should work correctly."""
        c = TexasProcurementConnector()
        results, metadata = c.discover(state="TX", city="Dallas", limit=20)
        assert all(r["city"] == "Dallas" for r in results)
        assert metadata["filtered_count"] > 0

    def test_discover_limits_results(self):
        """Limit parameter should cap results."""
        c = TexasProcurementConnector()
        results, metadata = c.discover(state="TX", limit=3)
        assert len(results) <= 3

    def test_discover_houston_cities(self):
        """Houston should have multiple companies."""
        c = TexasProcurementConnector()
        results, metadata = c.discover(state="TX", city="Houston", limit=50)
        assert metadata["filtered_count"] >= 5
        assert all(r["city"] == "Houston" for r in results)

    def test_discover_austin_cities(self):
        """Austin should have at least one company."""
        c = TexasProcurementConnector()
        results, metadata = c.discover(state="TX", city="Austin", limit=50)
        assert metadata["filtered_count"] >= 1
        assert all(r["city"] == "Austin" for r in results)

    def test_discover_san_antonio_cities(self):
        """San Antonio should have companies."""
        c = TexasProcurementConnector()
        results, metadata = c.discover(state="TX", city="San Antonio", limit=50)
        assert metadata["filtered_count"] >= 3

    def test_discover_industry_filter(self):
        """Industry keyword should filter results."""
        c = TexasProcurementConnector()
        results, _ = c.discover(state="TX", industry="commercial", limit=50)
        # Should find companies with "commercial" in their focus
        assert len(results) > 0

    def test_discover_industry_no_match(self):
        """Industry phrase with no matching keyword in any record."""
        c = TexasProcurementConnector()
        results, metadata = c.discover(
            state="TX",
            city="Dallas",
            industry="AstroNauticalPropulsion xyz123!@#",
            limit=10,
        )
        assert len(results) == 0

    def test_discover_metadata_structure(self):
        """Metadata must contain expected keys."""
        c = TexasProcurementConnector()
        _, metadata = c.discover(state="TX", limit=5)
        required_keys = {
            "connector",
            "total_records",
            "filtered_count",
            "returned_count",
            "filters_applied",
        }
        assert required_keys.issubset(set(metadata.keys()))

    def test_discover_empty_state(self):
        """Invalid state should return no results."""
        c = TexasProcurementConnector()
        results, metadata = c.discover(state="ZZ", limit=10)
        assert len(results) == 0

    def test_discover_preserves_company_structure(self):
        """Each result must have the expected output shape."""
        c = TexasProcurementConnector()
        results, _ = c.discover(state="TX", city="Dallas", limit=3)
        for r in results:
            assert "company_name" in r
            assert "website" in r
            assert "city" in r
            assert "state" in r
            assert "country" in r
            assert "source_url" in r


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestConstructionSourceRegistry:

    def test_list_connectors(self):
        connectors = list_connectors()
        names = [c["name"] for c in connectors]
        assert "texas_procurement" in names

    def test_get_connector(self):
        c = get_connector("texas_procurement")
        assert c is not None
        assert isinstance(c, TexasProcurementConnector)

    def test_get_unknown_connector(self):
        c = get_connector("nonexistent")
        assert c is None

    def test_registry_names(self):
        names = ConstructionSourceRegistry.list_names()
        assert "texas_procurement" in names


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestTexasProcurementAPI:

    def test_endpoint_exists(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        r = client.options(
            "/api/v1/connectors/texas-procurement", params={"state": "TX"}
        )
        assert r.status_code != 404

    @pytest.mark.network
    def test_endpoint_returns_200(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        r = client.get(
            "/api/v1/connectors/texas-procurement", params={"state": "TX", "limit": 5}
        )
        assert r.status_code == 200

    @pytest.mark.network
    def test_endpoint_returns_companies(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        r = client.get(
            "/api/v1/connectors/texas-procurement",
            params={"state": "TX", "city": "Dallas", "limit": 5},
        )
        data = r.json()
        assert "companies" in data
        assert len(data["companies"]) > 0
        for c in data["companies"]:
            assert "company_name" in c
            assert "website" in c

    def test_endpoint_validates_state_param(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        r = client.get(
            "/api/v1/connectors/texas-procurement", params={"state": "X", "limit": 5}
        )
        assert r.status_code == 422

    def test_endpoint_validates_limit(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        r = client.get(
            "/api/v1/connectors/texas-procurement", params={"state": "TX", "limit": 0}
        )
        assert r.status_code == 422

    @pytest.mark.network
    def test_endpoint_response_schema(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        r = client.get(
            "/api/v1/connectors/texas-procurement",
            params={"state": "TX", "city": "Houston", "limit": 3},
        )
        data = r.json()
        assert data["connector"] == "texas_procurement"
        assert isinstance(data["companies"], list)
        for c in data["companies"]:
            assert "company_name" in c
            assert "website" in c
            assert "city" in c
            assert "state" in c
            assert "country" in c
            assert "source_url" in c

    def test_summary_endpoint(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        r = client.get("/api/v1/connectors/texas-procurement/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["connector"] == "texas_procurement"
        assert data["health_check"] is True
        assert isinstance(data["total_records"], int)
        assert data["total_records"] >= 40
        assert data["bridge_mode"] is True
