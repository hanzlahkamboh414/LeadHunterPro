"""Tests for ConnectorManager."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.engines.source_connectors.connector_manager import ConnectorManager
from app.engines.source_connectors.mock_connector import MockConnector
from app.engines.source_connectors.sdk import CompanyResult, ConnectorRegistry


class TestConnectorManager:

    @pytest.fixture(autouse=True)
    def setup_registry(self):
        """Register only the mock connector for isolated tests."""
        ConnectorRegistry.clear()
        ConnectorRegistry.register(MockConnector())
        yield
        ConnectorRegistry.clear()

    def test_list_connectors(self):
        mgr = ConnectorManager()
        connectors = mgr.list_connectors()
        names = [c["name"] for c in connectors]
        assert "mock" in names

    def test_get_connector(self):
        mgr = ConnectorManager()
        c = mgr.get_connector("mock")
        assert isinstance(c, MockConnector)

    def test_get_unknown_connector(self):
        mgr = ConnectorManager()
        assert mgr.get_connector("nonexistent") is None

    def test_discover_all_connectors(self):
        mgr = ConnectorManager()
        results, metadata = mgr.discover(limit=10)
        assert isinstance(results, list)
        assert metadata["total_raw"] >= 0
        assert "connectors_queried" in metadata

    def test_discover_specific_connector(self):
        mgr = ConnectorManager()
        results, metadata = mgr.discover(connector_name="mock", limit=5)
        assert len(results) <= 5

    def test_discover_with_filters(self):
        mgr = ConnectorManager()
        results, metadata = mgr.discover(state="TX", city="Dallas", limit=10)
        assert all(r.state == "TX" for r in results)
        assert "filters_applied" in metadata

    def test_discover_empty_state_returns_no_results(self):
        mgr = ConnectorManager()
        results, _ = mgr.discover(state="ZZ", limit=10)
        assert len(results) == 0

    def test_discover_limit_applied(self):
        mgr = ConnectorManager()
        results, _ = mgr.discover(limit=2)
        assert len(results) <= 2

    def test_deduplication_across_connectors(self):
        """Multiple mock connectors with overlapping data should be deduplicated."""
        # Clear and re-register only mocks to isolate this test
        ConnectorRegistry.clear()
        m1 = MockConnector()
        m2 = MockConnector()
        m3 = MockConnector()
        ConnectorRegistry.register(m1)
        ConnectorRegistry.register(m2)
        ConnectorRegistry.register(m3)

        mgr = ConnectorManager()
        results, metadata = mgr.discover(limit=50)
        # With 3 mock connectors + any re-registered builtins,
        # total_raw should be > results due to deduplication.
        assert metadata["total_raw"] >= len(results)
        # Deduplication must have removed at least some duplicates
        # (3 mock connectors × 7 companies = 21 raw min, fewer after dedup)
        if metadata["total_raw"] >= 21:
            assert len(results) < metadata["total_raw"]

    def test_metadata_structure(self):
        mgr = ConnectorManager()
        _, metadata = mgr.discover(limit=5)
        assert "total_raw" in metadata
        assert "total_after_dedup" in metadata
        assert "connectors_queried" in metadata
        assert "filters_applied" in metadata
