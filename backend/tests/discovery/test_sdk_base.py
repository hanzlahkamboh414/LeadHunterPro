"""Base tests for the Connector SDK."""

from __future__ import annotations

import json
import pytest
from unittest.mock import patch, MagicMock

from app.engines.source_connectors.sdk import (
    BaseConnector,
    CompanyResult,
    ConnectorConfig,
    ConnectorRegistry,
)
from app.engines.source_connectors.sdk_utils import (
    ConnectorLogger,
    connector_retry,
    ConnectorErrorHandler,
    HTTPClient,
)
from app.engines.source_connectors.example_connector import ExampleConnector, ensure_fixture


# ---------------------------------------------------------------------------
# CompanyResult tests
# ---------------------------------------------------------------------------


class TestCompanyResult:

    def test_create_with_defaults(self):
        result = CompanyResult(
            company_name="Test Co",
            website="https://test.com",
            city="Dallas",
            state="TX",
        )
        assert result.company_name == "Test Co"
        assert result.country == "USA"
        assert result.source_url == ""
        assert result.extra == {}

    def test_from_dict(self):
        data = {
            "company_name": "Acme Corp",
            "website": "https://acme.com",
            "city": "Houston",
            "state": "TX",
            "country": "USA",
            "source_url": "https://acme.com/about",
            "industry_focus": "Commercial",
            "revenue_tier": "enterprise",
            "extra_field": "extra_value",
        }
        result = CompanyResult.from_dict(data)
        assert result.company_name == "Acme Corp"
        assert result.extra == {"extra_field": "extra_value"}
        assert "extra_field" not in dir(result)  # extra fields go to .extra dict

    def test_to_dict(self):
        result = CompanyResult(
            company_name="Test",
            website="https://test.com",
            city="Dallas",
            state="TX",
        )
        d = result.to_dict()
        assert d["company_name"] == "Test"
        assert d["state"] == "TX"

    def test_is_frozen(self):
        result = CompanyResult(
            company_name="Test",
            website="https://test.com",
            city="Dallas",
            state="TX",
        )
        with pytest.raises((AttributeError, TypeError)):
            result.company_name = "Modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ConnectorConfig tests
# ---------------------------------------------------------------------------


class TestConnectorConfig:

    def test_default_values(self):
        config = ConnectorConfig(name="test_connector")
        assert config.name == "test_connector"
        assert config.timeout == 30
        assert config.max_retries == 3
        assert config.user_agent == "LeadHunterPro/1.0 (contact@leadhunterpro.ai)"
        assert config.rate_limit == 0.0
        assert config.auth is None

    def test_custom_values(self):
        config = ConnectorConfig(
            name="my_connector",
            timeout=60,
            max_retries=5,
            rate_limit=2.0,
            auth={"api_key": "secret"},
        )
        assert config.timeout == 60
        assert config.max_retries == 5
        assert config.rate_limit == 2.0
        assert config.auth == {"api_key": "secret"}


# ---------------------------------------------------------------------------
# BaseConnector tests
# ---------------------------------------------------------------------------


class TestBaseConnector:

    def test_subclass_must_implement_discover(self):
        """Subclasses that don't implement discover() cannot be instantiated."""
        with pytest.raises(TypeError):
            class IncompleteConnector(BaseConnector):
                @property
                def name(self):
                    return "incomplete"

                @property
                def description(self):
                    return "Incomplete"

                # Missing discover() implementation
            IncompleteConnector()  # Should raise TypeError

    def test_base_discover_raises_not_implemented(self):
        """Calling discover() on base class should raise NotImplementedError."""
        connector = BaseConnector.__new__(BaseConnector)
        connector._config = ConnectorConfig(name="base")
        from app.engines.source_connectors.sdk_utils import ConnectorLogger
        connector._logger = ConnectorLogger("base")
        with pytest.raises(NotImplementedError):
            connector.discover()


# ---------------------------------------------------------------------------
# ConnectorRegistry tests
# ---------------------------------------------------------------------------


class TestConnectorRegistry:

    def test_register_and_get(self):
        connector = ExampleConnector()
        ConnectorRegistry.register(connector)
        assert ConnectorRegistry.get("example") is connector

    def test_list_names(self):
        ConnectorRegistry.list_names()  # Should not raise

    def test_list_all(self):
        connectors = ConnectorRegistry.list_all()
        assert isinstance(connectors, list)

    def test_get_unknown_returns_none(self):
        assert ConnectorRegistry.get("nonexistent") is None


# ---------------------------------------------------------------------------
# SDK Utils tests
# ---------------------------------------------------------------------------


class TestConnectorLogger:

    def test_logger_methods_exist(self):
        log = ConnectorLogger("test")
        assert hasattr(log, "info")
        assert hasattr(log, "warning")
        assert hasattr(log, "error")
        assert hasattr(log, "debug")

    def test_error_with_exception(self):
        log = ConnectorLogger("test")
        # Should not raise
        log.error("test error", exc=ValueError("oops"))


class TestConnectorRetry:

    def test_retry_success_on_first_try(self):
        calls = []

        @connector_retry(max_retries=3)
        def success_func():
            calls.append(1)
            return "ok"

        result = success_func()
        assert result == "ok"
        assert len(calls) == 1

    def test_retry_on_failure_then_success(self):
        calls = []

        @connector_retry(max_retries=2, exceptions=(ValueError,))
        def flaky_func():
            calls.append(1)
            if len(calls) < 2:
                raise ValueError("try again")
            return "ok"

        result = flaky_func()
        assert result == "ok"
        assert len(calls) == 2

    def test_retry_exhausted_raises(self):
        call_count = [0]

        @connector_retry(max_retries=1, exceptions=(RuntimeError,))
        def always_fail():
            call_count[0] += 1
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            always_fail()
        assert call_count[0] == 2  # original + 1 retry


class TestConnectorErrorHandler:

    def test_add_error(self):
        handler = ConnectorErrorHandler("test")
        handler.add_error("fetch", "connection refused")
        assert handler.has_errors
        assert len(handler.errors) == 1
        assert handler.errors[0]["operation"] == "fetch"

    def test_add_warning(self):
        handler = ConnectorErrorHandler("test")
        handler.add_warning("rate limit approaching")
        assert len(handler.warnings) == 1

    def test_summary(self):
        handler = ConnectorErrorHandler("test")
        handler.add_error("fetch", "timeout")
        handler.add_warning("slow response")
        summary = handler.summary()
        assert summary["connector"] == "test"
        assert summary["error_count"] == 1
        assert summary["warning_count"] == 1

    def test_no_errors_initially(self):
        handler = ConnectorErrorHandler("test")
        assert not handler.has_errors


class TestHTTPClient:

    def test_context_manager(self):
        with HTTPClient(timeout=5) as client:
            assert client._timeout == 5

    def test_close(self):
        client = HTTPClient()
        client.close()  # Should not raise


# ---------------------------------------------------------------------------
# ExampleConnector integration tests
# ---------------------------------------------------------------------------


class TestExampleConnector:

    def test_connector_name(self):
        c = ExampleConnector()
        assert c.name == "example"

    def test_connector_description(self):
        c = ExampleConnector()
        assert "example" in c.description.lower()

    def test_discover_returns_companies(self):
        c = ExampleConnector()
        results, metadata = c.discover(state="TX", limit=10)
        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, CompanyResult) for r in results)

    def test_discover_state_filter(self):
        c = ExampleConnector()
        results, _ = c.discover(state="TX", limit=10)
        assert all(r.state == "TX" for r in results)

    def test_discover_city_filter(self):
        c = ExampleConnector()
        results, _ = c.discover(city="Dallas", limit=10)
        assert all(r.city == "Dallas" for r in results)

    def test_discover_empty_when_no_match(self):
        c = ExampleConnector()
        results, _ = c.discover(state="ZZ", limit=10)
        assert len(results) == 0

    def test_discover_metadata_structure(self):
        c = ExampleConnector()
        _, metadata = c.discover(limit=5)
        assert "connector" in metadata
        assert "returned_count" in metadata
        assert "filters_applied" in metadata

    def test_company_result_structure(self):
        c = ExampleConnector()
        results, _ = c.discover(limit=1)
        r = results[0]
        assert isinstance(r, CompanyResult)
        assert r.company_name != ""
        assert r.website != ""
        assert r.city != ""
        assert r.state != ""


# ---------------------------------------------------------------------------
# Fixture file tests
# ---------------------------------------------------------------------------


class TestFixture:

    def test_fixture_exists(self):
        from pathlib import Path
        # Use a relative path from this file's location
        fixture_path = Path(__file__).parent.parent.parent / "app" / "engines" / "source_connectors" / "_fixtures" / "example_companies.json"
        assert fixture_path.exists(), f"Fixture not found at {fixture_path}"

    def test_fixture_is_valid_json(self):
        import json
        from pathlib import Path
        fixture_path = Path(__file__).parent.parent.parent / "app" / "engines" / "source_connectors" / "_fixtures" / "example_companies.json"
        with open(fixture_path) as f:
            data = json.load(f)
        assert isinstance(data, list)
        assert len(data) > 0
