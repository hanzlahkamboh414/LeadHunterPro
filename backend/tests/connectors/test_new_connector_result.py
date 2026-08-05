"""Tests for ConnectorResult dataclass."""

from __future__ import annotations

import pytest

from app.connectors.connector_result import ConnectorResult


class TestConnectorResult:

    def test_create_with_defaults(self):
        result = ConnectorResult(
            company_name="Test Co",
            website="https://test.com",
            city="Dallas",
            state="TX",
        )
        assert result.company_name == "Test Co"
        assert result.country == "USA"
        assert result.source == ""
        assert result.confidence == 1.0
        assert result.metadata == {}

    def test_create_with_all_fields(self):
        result = ConnectorResult(
            company_name="Acme Corp",
            website="https://acme.com",
            city="Houston",
            state="TX",
            country="USA",
            source="texas_procurement",
            source_url="https://acme.com/about",
            confidence=0.95,
            metadata={"revenue_tier": "enterprise"},
        )
        assert result.source == "texas_procurement"
        assert result.confidence == 0.95
        assert result.metadata == {"revenue_tier": "enterprise"}

    def test_from_dict(self):
        data = {
            "company_name": "Acme Corp",
            "website": "https://acme.com",
            "city": "Houston",
            "state": "TX",
            "source": "mock",
            "confidence": 0.8,
        }
        result = ConnectorResult.from_dict(data)
        assert result.company_name == "Acme Corp"
        assert result.source == "mock"
        assert result.confidence == 0.8

    def test_to_dict(self):
        result = ConnectorResult(
            company_name="Test",
            website="https://test.com",
            city="Dallas",
            state="TX",
            source="mock",
            confidence=0.75,
            metadata={"extra": "value"},
        )
        d = result.to_dict()
        assert d["company_name"] == "Test"
        assert d["source"] == "mock"
        assert d["confidence"] == 0.75
        assert d["extra"] == "value"

    def test_is_frozen(self):
        result = ConnectorResult(
            company_name="Test",
            website="https://test.com",
            city="Dallas",
            state="TX",
        )
        with pytest.raises((AttributeError, TypeError)):
            result.company_name = "Modified"  # type: ignore[misc]
