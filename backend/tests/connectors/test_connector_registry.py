"""Tests for ConnectorRegistry."""

from __future__ import annotations

import pytest

from app.connectors.base_connector import BaseConnector
from app.connectors.connector_registry import ConnectorRegistry


class DummyConnector(BaseConnector):
    """Dummy connector for testing."""

    connector_name = "dummy"
    priority = 50
    enabled = True

    def search(self, industry, location, limit):
        return [], {}

    def health_check(self):
        return True

    def validate_result(self, result):
        return True


class TestConnectorRegistry:

    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        ConnectorRegistry.clear()
        yield
        ConnectorRegistry.clear()

    def test_register_and_get(self):
        conn = DummyConnector()
        ConnectorRegistry.register(conn)
        assert ConnectorRegistry.get("dummy") is conn

    def test_register_duplicate_overwrites(self):
        conn1 = DummyConnector()
        conn2 = DummyConnector()
        conn2.connector_name = "dummy"
        ConnectorRegistry.register(conn1)
        ConnectorRegistry.register(conn2)
        assert ConnectorRegistry.get("dummy") is conn2

    def test_remove(self):
        conn = DummyConnector()
        ConnectorRegistry.register(conn)
        assert ConnectorRegistry.remove("dummy") is True
        assert ConnectorRegistry.get("dummy") is None

    def test_remove_unknown(self):
        assert ConnectorRegistry.remove("nonexistent") is False

    def test_enable(self):
        conn = DummyConnector()
        ConnectorRegistry.register(conn)
        assert ConnectorRegistry.enable("dummy") is True
        assert conn.enabled is True

    def test_enable_unknown(self):
        assert ConnectorRegistry.enable("nonexistent") is False

    def test_disable(self):
        conn = DummyConnector()
        ConnectorRegistry.register(conn)
        conn.enabled = False
        assert ConnectorRegistry.disable("dummy") is True
        assert conn.enabled is False

    def test_disable_unknown(self):
        assert ConnectorRegistry.disable("nonexistent") is False

    def test_get_all(self):
        conn1 = DummyConnector()
        conn2 = DummyConnector()
        conn2.connector_name = "dummy2"
        ConnectorRegistry.register(conn1)
        ConnectorRegistry.register(conn2)
        all_connectors = ConnectorRegistry.get_all()
        assert len(all_connectors) == 2

    def test_get_enabled_filters_disabled(self):
        conn1 = DummyConnector()
        conn2 = DummyConnector()
        conn2.connector_name = "dummy2"
        conn2.enabled = False
        ConnectorRegistry.register(conn1)
        ConnectorRegistry.register(conn2)
        enabled = ConnectorRegistry.get_enabled()
        assert len(enabled) == 1
        assert enabled[0].connector_name == "dummy"

    def test_get_enabled_sorted_by_priority(self):
        class HighPriority(BaseConnector):
            connector_name = "high"
            priority = 10
            enabled = True

            def search(self, *a, **k):
                return [], {}

            def health_check(self):
                return True

            def validate_result(self, r):
                return True

        class LowPriority(BaseConnector):
            connector_name = "low"
            priority = 90
            enabled = True

            def search(self, *a, **k):
                return [], {}

            def health_check(self):
                return True

            def validate_result(self, r):
                return True

        ConnectorRegistry.register(LowPriority())
        ConnectorRegistry.register(HighPriority())
        enabled = ConnectorRegistry.get_enabled()
        assert enabled[0].connector_name == "high"
        assert enabled[1].connector_name == "low"

    def test_clear(self):
        conn = DummyConnector()
        ConnectorRegistry.register(conn)
        ConnectorRegistry.clear()
        assert len(ConnectorRegistry.get_all()) == 0
