"""Tests for BaseConnector abstract class."""

from __future__ import annotations

import pytest

from app.connectors.base_connector import BaseConnector
from app.connectors.connector_result import ConnectorResult


class ConcreteConnector(BaseConnector):
    """A concrete implementation of BaseConnector for testing."""

    connector_name = "test_connector"
    priority = 10
    enabled = True

    def search(self, industry, location, limit):
        return [
            ConnectorResult(
                company_name="Test Company",
                website="https://test.com",
                city="Dallas",
                state="TX",
                source=self.connector_name,
            )
        ], {"total": 1}

    def health_check(self):
        return True

    def validate_result(self, result):
        return bool(result.company_name and result.website)


class TestBaseConnector:

    def test_concrete_implementation(self):
        conn = ConcreteConnector()
        assert conn.connector_name == "test_connector"
        assert conn.priority == 10
        assert conn.enabled is True
        assert conn.health_check() is True

    def test_search_returns_results(self):
        conn = ConcreteConnector()
        results, metadata = conn.search("Construction", "TX", limit=10)
        assert len(results) == 1
        assert isinstance(results[0], ConnectorResult)

    def test_validate_result_accepts_valid(self):
        conn = ConcreteConnector()
        result = ConnectorResult(
            company_name="Valid Co",
            website="https://valid.com",
            city="A",
            state="B",
        )
        assert conn.validate_result(result) is True

    def test_validate_result_rejects_invalid(self):
        conn = ConcreteConnector()
        result = ConnectorResult(
            company_name="",
            website="https://invalid.com",
            city="A",
            state="B",
        )
        assert conn.validate_result(result) is False

    def test_base_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            BaseConnector()  # type: ignore[abstract]

    def test_missing_abstract_methods_raises(self):
        class IncompleteConnector(BaseConnector):
            connector_name = "incomplete"
            priority = 100
            enabled = True
            # Missing search, health_check, validate_result

        with pytest.raises(TypeError):
            IncompleteConnector()  # type: ignore[abstract]
