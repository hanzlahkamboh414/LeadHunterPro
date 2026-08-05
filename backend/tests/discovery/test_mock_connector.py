"""Tests for MockConnector."""

from __future__ import annotations

import pytest

from app.engines.source_connectors.mock_connector import MockConnector
from app.engines.source_connectors.sdk import CompanyResult, ConnectorConfig


class TestMockConnector:

    def test_connector_name(self):
        c = MockConnector()
        assert c.name == "mock"

    def test_connector_description(self):
        c = MockConnector()
        assert "mock" in c.description.lower()

    def test_discover_returns_companies(self):
        c = MockConnector()
        results, metadata = c.discover(limit=10)
        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, CompanyResult) for r in results)

    def test_discover_state_filter(self):
        c = MockConnector()
        results, _ = c.discover(state="TX", limit=10)
        assert all(r.state == "TX" for r in results)

    def test_discover_city_filter(self):
        c = MockConnector()
        results, _ = c.discover(city="Dallas", limit=10)
        assert all(r.city == "Dallas" for r in results)

    def test_discover_empty_when_no_match(self):
        c = MockConnector()
        results, _ = c.discover(state="ZZ", limit=10)
        assert len(results) == 0

    def test_discover_metadata_structure(self):
        c = MockConnector()
        _, metadata = c.discover(limit=5)
        assert "connector" in metadata
        assert "returned_count" in metadata
        assert "filters_applied" in metadata
        assert metadata["is_mock"] is True

    def test_company_result_structure(self):
        c = MockConnector()
        results, _ = c.discover(limit=1)
        r = results[0]
        assert isinstance(r, CompanyResult)
        assert r.company_name != ""
        assert r.website != ""
        assert r.city != ""
        assert r.state != ""

    def test_is_available(self):
        c = MockConnector()
        assert c.is_available() is True

    def test_custom_config(self):
        config = ConnectorConfig(name="my_mock", timeout=10)
        c = MockConnector(config=config)
        # MockConnector always uses "mock" as its canonical name
        assert c.name == "mock"
        # But config is still stored correctly
        assert c._config.timeout == 10

    def test_limit_cap(self):
        c = MockConnector()
        results, _ = c.discover(limit=2)
        assert len(results) <= 2
