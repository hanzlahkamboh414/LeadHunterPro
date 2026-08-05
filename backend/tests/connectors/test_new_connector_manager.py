"""Tests for ConnectorManager."""

from __future__ import annotations

import pytest

from app.connectors.base_connector import BaseConnector
from app.connectors.connector_manager import ConnectorManager
from app.connectors.connector_registry import ConnectorRegistry
from app.connectors.connector_result import ConnectorResult


class MockConnector(BaseConnector):
    """Mock connector for testing."""

    connector_name = "mock"
    priority = 10
    enabled = True

    def __init__(self, results=None, should_fail=False):
        self._results = results or []
        self._should_fail = should_fail

    def search(self, industry, location, limit):
        if self._should_fail:
            raise RuntimeError("Simulated failure")
        return self._results, {"total": len(self._results)}

    def health_check(self):
        return not self._should_fail

    def validate_result(self, result):
        return bool(result.company_name)


class TestConnectorManager:

    @pytest.fixture(autouse=True)
    def setup_registry(self):
        ConnectorRegistry.clear()
        yield
        ConnectorRegistry.clear()

    def test_discover_with_results(self):
        conn = MockConnector(
            results=[
                ConnectorResult(
                    company_name="Alpha",
                    website="https://alpha.com",
                    city="Dallas",
                    state="TX",
                    source="mock",
                ),
                ConnectorResult(
                    company_name="Beta",
                    website="https://beta.com",
                    city="Houston",
                    state="TX",
                    source="mock",
                ),
            ]
        )
        ConnectorRegistry.register(conn)

        manager = ConnectorManager()
        results, metadata = manager.discover(
            industry="Construction", location="TX", limit=10
        )

        assert len(results) == 2
        assert metadata["connectors_executed"] == 1
        assert metadata["total_raw"] == 2

    def test_discover_empty_when_no_connectors(self):
        manager = ConnectorManager()
        results, metadata = manager.discover(
            industry="Construction", location="TX", limit=10
        )

        assert results == []
        assert metadata["connectors_executed"] == 0

    def test_discover_skips_failed_connectors(self):
        failing_conn = MockConnector(should_fail=True)
        ConnectorRegistry.register(failing_conn)

        manager = ConnectorManager()
        results, metadata = manager.discover(
            industry="Construction", location="TX", limit=10
        )

        assert results == []
        assert "mock" in metadata["errors"]

    def test_discover_respects_limit(self):
        conn = MockConnector(
            results=[
                ConnectorResult(
                    company_name=f"Company{i}",
                    website=f"https://company{i}.com",
                    city="Dallas",
                    state="TX",
                    source="mock",
                )
                for i in range(10)
            ]
        )
        ConnectorRegistry.register(conn)

        manager = ConnectorManager()
        results, _ = manager.discover(industry="Construction", location="TX", limit=5)

        assert len(results) <= 5

    def test_deduplication_by_domain(self):
        conn = MockConnector(
            results=[
                ConnectorResult(
                    company_name="Alpha",
                    website="https://alpha.com",
                    city="Dallas",
                    state="TX",
                    source="mock",
                ),
                ConnectorResult(
                    company_name="Alpha Branch",
                    website="https://alpha.com/locations/dallas",
                    city="Dallas",
                    state="TX",
                    source="mock",
                ),
            ]
        )
        ConnectorRegistry.register(conn)

        manager = ConnectorManager()
        results, _ = manager.discover(industry="Construction", location="TX", limit=10)

        # Should deduplicate by domain - both have same domain alpha.com
        assert len(results) == 1

    def test_priority_ordering(self):
        class HighPriority(MockConnector):
            connector_name = "high"
            priority = 10

        class LowPriority(MockConnector):
            connector_name = "low"
            priority = 90

        ConnectorRegistry.register(LowPriority())
        ConnectorRegistry.register(HighPriority())

        manager = ConnectorManager()
        results, metadata = manager.discover(
            industry="Construction", location="TX", limit=10
        )

        # Both connectors should run
        assert metadata["connectors_executed"] == 2
