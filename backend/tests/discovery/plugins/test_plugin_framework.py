"""Tests for the discovery plugin framework (Phase 2.1).

Covers the framework contract:
registration, unregister, duplicate registration, disabled plugins,
priority ordering, metrics update, health update — plus the Phase 2.1
improvements: the shared singleton registry, the expanded config,
open-ended capabilities, and multi-state health.

No real discovery sources are involved — every plugin here is a stub
defined in this file.
"""

from __future__ import annotations

import asyncio

import pytest

from app.discovery.plugins.base_plugin import (
    BaseDiscoveryPlugin,
    PluginCapability,
    normalize_capability,
)
from app.discovery.plugins.plugin_config import PluginConfig
from app.discovery.plugins.plugin_health import PluginHealth, PluginState
from app.discovery.plugins.plugin_manager import PluginManager
from app.discovery.plugins.plugin_metrics import PluginMetrics
from app.discovery.plugins.plugin_registry import (
    PluginRegistry,
    get_registry,
    plugin_registry,
)
from app.discovery.sources.status import SourceHealth, SourceStatus


# ----------------------------------------------------------------------
# Stub plugins
# ----------------------------------------------------------------------


class StubPlugin(BaseDiscoveryPlugin):
    """Minimal plugin that returns whatever it is told to return."""

    name = "stub"
    description = "Stub plugin for framework tests"
    priority = 50
    enabled = True
    capabilities = (PluginCapability.COMPANY_DISCOVERY,)

    def __init__(
        self,
        config=None,
        *,
        status=SourceStatus.SUCCESS,
        companies=None,
        metadata=None,
    ):
        super().__init__(config)
        self._status = status
        self._companies = companies if companies is not None else [
            {"company_name": "Stub Co", "website": "https://stub.test"}
        ]
        self._metadata = metadata or {}
        self.discover_calls = 0

    def discover(self, *, industry, location, limit):
        self.discover_calls += 1
        meta = {"query": industry, **self._metadata}
        return self._status, list(self._companies[:limit]), meta


class SecondStubPlugin(StubPlugin):
    """A differently-named stub, for multi-plugin tests."""

    name = "stub_two"
    priority = 10


class RaisingPlugin(BaseDiscoveryPlugin):
    """Plugin whose discover() always raises."""

    name = "raiser"
    priority = 20
    capabilities = (PluginCapability.COMPANY_DISCOVERY,)

    def discover(self, *, industry, location, limit):
        raise RuntimeError("boom")


class UnhealthyPlugin(BaseDiscoveryPlugin):
    """Plugin that reports itself unhealthy."""

    name = "unhealthy"
    priority = 30
    capabilities = (PluginCapability.NEWS_DISCOVERY,)

    def discover(self, *, industry, location, limit):
        return SourceStatus.EMPTY, [], {}

    async def health_check(self):
        return {"healthy": False, "plugin": self.name, "status": "unreachable"}


class MaintenancePlugin(BaseDiscoveryPlugin):
    """Plugin that reports a fine-grained state instead of a boolean."""

    name = "maintenance"
    priority = 40
    capabilities = (PluginCapability.PERMIT_DISCOVERY,)

    def discover(self, *, industry, location, limit):
        return SourceStatus.EMPTY, [], {}

    async def health_check(self):
        return {"healthy": False, "state": "maintenance", "status": "migrating"}


class CustomCapabilityPlugin(BaseDiscoveryPlugin):
    """Declares a capability the framework has never heard of."""

    name = "custom_cap"
    priority = 15
    capabilities = ("patent_discovery",)

    def discover(self, *, industry, location, limit):
        return SourceStatus.EMPTY, [], {}


class NamelessPlugin(BaseDiscoveryPlugin):
    """Plugin that forgets to declare a name."""

    def discover(self, *, industry, location, limit):
        return SourceStatus.EMPTY, [], {}


@pytest.fixture
def registry():
    """An isolated registry, so tests never touch global state."""
    return PluginRegistry()


@pytest.fixture
def manager(registry):
    """A manager bound to an isolated registry."""
    return PluginManager(registry)


@pytest.fixture(autouse=True)
def clean_shared_registry():
    """Keep the process-wide registry empty around every test."""
    PluginRegistry.reset_instance()
    yield
    PluginRegistry.reset_instance()


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------


class TestPluginRegistration:

    def test_register_and_get(self, registry):
        plugin = StubPlugin()
        assert registry.register(plugin) is True
        assert registry.get("stub") is plugin
        assert len(registry) == 1
        assert "stub" in registry

    def test_register_rejects_non_plugin(self, registry):
        with pytest.raises(TypeError):
            registry.register(object())

    def test_plugin_without_name_cannot_be_constructed(self):
        with pytest.raises(ValueError):
            NamelessPlugin()

    def test_get_unknown_returns_none(self, registry):
        assert registry.get("nope") is None

    def test_names_are_priority_ordered(self, registry):
        registry.register(StubPlugin())          # priority 50
        registry.register(SecondStubPlugin())    # priority 10
        assert registry.names() == ["stub_two", "stub"]

    def test_manager_register_creates_tracking_records(self, manager):
        manager.register(StubPlugin())
        assert isinstance(manager.get_metrics("stub"), PluginMetrics)
        assert isinstance(manager.get_health("stub"), PluginHealth)

    def test_manager_adopts_preexisting_registry_plugins(self, registry):
        registry.register(StubPlugin())
        adopting = PluginManager(registry)
        assert adopting.get_metrics("stub") is not None
        assert adopting.get_health("stub") is not None


# ----------------------------------------------------------------------
# Shared singleton registry
# ----------------------------------------------------------------------


class TestSharedRegistry:

    def test_get_instance_is_the_same_object(self):
        assert PluginRegistry.get_instance() is PluginRegistry.get_instance()

    def test_get_registry_matches_get_instance(self):
        assert get_registry() is PluginRegistry.get_instance()

    def test_module_alias_is_the_shared_instance(self):
        assert plugin_registry is PluginRegistry.get_instance()

    def test_manager_defaults_to_shared_registry(self):
        assert PluginManager().registry is get_registry()

    def test_registration_is_visible_through_every_handle(self):
        get_registry().register(StubPlugin())
        assert PluginRegistry.get_instance().get("stub") is not None
        assert plugin_registry.get("stub") is not None
        assert PluginManager().registry.get("stub") is not None

    def test_reset_instance_does_not_rebind_the_singleton(self):
        before = PluginRegistry.get_instance()
        before.register(StubPlugin())
        PluginRegistry.reset_instance()
        assert PluginRegistry.get_instance() is before
        assert plugin_registry is before
        assert len(before) == 0

    def test_explicit_instance_is_isolated_from_the_singleton(self, registry):
        registry.register(StubPlugin())
        assert len(registry) == 1
        assert len(get_registry()) == 0


# ----------------------------------------------------------------------
# Unregister
# ----------------------------------------------------------------------


class TestPluginUnregister:

    def test_unregister_removes_plugin(self, registry):
        registry.register(StubPlugin())
        assert registry.unregister("stub") is True
        assert registry.get("stub") is None
        assert len(registry) == 0

    def test_unregister_unknown_returns_false(self, registry):
        assert registry.unregister("nope") is False

    def test_clear_removes_everything(self, registry):
        registry.register(StubPlugin())
        registry.register(SecondStubPlugin())
        registry.clear()
        assert len(registry) == 0

    def test_manager_unregister_drops_tracking_records(self, manager):
        manager.register(StubPlugin())
        assert manager.unregister("stub") is True
        assert manager.get_metrics("stub") is None
        assert manager.get_health("stub") is None

    def test_manager_unregister_unknown_returns_false(self, manager):
        assert manager.unregister("nope") is False


# ----------------------------------------------------------------------
# Duplicate registration
# ----------------------------------------------------------------------


class TestDuplicateRegistration:

    def test_duplicate_is_skipped_not_raised(self, registry):
        registry.register(StubPlugin())
        assert registry.register(StubPlugin()) is False

    def test_duplicate_keeps_the_original_plugin(self, registry):
        first = StubPlugin()
        second = StubPlugin()
        registry.register(first)
        registry.register(second)
        assert registry.get("stub") is first
        assert registry.get("stub") is not second
        assert len(registry) == 1

    def test_duplicate_logs_a_warning(self, registry, caplog):
        registry.register(StubPlugin())
        with caplog.at_level("WARNING"):
            registry.register(StubPlugin())
        assert "already registered" in caplog.text
        assert "stub" in caplog.text

    def test_replace_true_overwrites(self, registry):
        first = StubPlugin()
        second = StubPlugin()
        registry.register(first)
        assert registry.register(second, replace=True) is True
        assert registry.get("stub") is second
        assert len(registry) == 1

    def test_manager_reports_skipped_duplicate(self, manager):
        assert manager.register(StubPlugin()) is True
        assert manager.register(StubPlugin()) is False

    def test_skipped_duplicate_does_not_reset_metrics(self, manager):
        manager.register(StubPlugin())
        manager.discover_all(industry="Roofing", location="Dallas", limit=10)
        assert manager.get_metrics("stub").total_runs == 1

        manager.register(StubPlugin())  # skipped
        assert manager.get_metrics("stub").total_runs == 1


# ----------------------------------------------------------------------
# Disabled plugins
# ----------------------------------------------------------------------


class TestDisabledPlugins:

    def test_get_enabled_excludes_disabled(self, registry):
        enabled = StubPlugin()
        disabled = SecondStubPlugin()
        disabled.enabled = False
        registry.register(enabled)
        registry.register(disabled)

        result = registry.get_enabled()
        assert [p.name for p in result] == ["stub"]
        assert len(registry.get_all()) == 2

    def test_disable_and_enable(self, registry):
        registry.register(StubPlugin())
        assert registry.disable("stub") is True
        assert registry.get("stub").enabled is False
        assert registry.get("stub").config.enabled is False
        assert registry.get_enabled() == []

        assert registry.enable("stub") is True
        assert registry.get("stub").enabled is True
        assert len(registry.get_enabled()) == 1

    def test_enable_disable_unknown_returns_false(self, registry):
        assert registry.enable("nope") is False
        assert registry.disable("nope") is False

    def test_config_disabled_overrides_class_attribute(self):
        plugin = StubPlugin(PluginConfig(name="stub", enabled=False))
        assert plugin.enabled is False

    def test_disabled_plugin_is_not_executed(self, manager):
        plugin = StubPlugin()
        manager.register(plugin)
        manager.registry.disable("stub")

        results, diagnostics = manager.discover_all(
            industry="Roofing", location="Dallas Texas", limit=10
        )
        assert results == []
        assert plugin.discover_calls == 0
        assert diagnostics["plugins_registered"] == 1
        assert diagnostics["plugins_selected"] == 0
        assert diagnostics["selection_reason"] == "all_registered_plugins_disabled"

    def test_empty_registry_is_reported_not_hidden(self, manager):
        results, diagnostics = manager.discover_all(
            industry="Roofing", location="Dallas Texas", limit=10
        )
        assert results == []
        assert diagnostics["plugins_registered"] == 0
        assert diagnostics["selection_reason"] == "no_plugins_registered"

    def test_unmatched_capability_is_reported(self, manager):
        manager.register(StubPlugin())  # COMPANY_DISCOVERY only
        results, diagnostics = manager.discover_all(
            industry="Roofing",
            location="Dallas",
            limit=10,
            capability=PluginCapability.BID_DISCOVERY,
        )
        assert results == []
        assert (
            diagnostics["selection_reason"]
            == "no_enabled_plugin_supports_bid_discovery"
        )


# ----------------------------------------------------------------------
# Priority ordering
# ----------------------------------------------------------------------


class TestPriorityOrdering:

    def test_get_all_sorted_by_priority(self, registry):
        registry.register(StubPlugin())          # 50
        registry.register(RaisingPlugin())       # 20
        registry.register(SecondStubPlugin())    # 10
        assert [p.name for p in registry.get_all()] == [
            "stub_two",
            "raiser",
            "stub",
        ]

    def test_get_enabled_sorted_by_priority(self, registry):
        registry.register(StubPlugin())          # 50
        registry.register(SecondStubPlugin())    # 10
        assert [p.name for p in registry.get_enabled()] == ["stub_two", "stub"]

    def test_registration_order_does_not_affect_ordering(self, registry):
        registry.register(SecondStubPlugin())    # 10, registered first
        registry.register(StubPlugin())          # 50
        assert [p.name for p in registry.get_all()] == ["stub_two", "stub"]

    def test_config_priority_overrides_class_attribute(self, registry):
        low = StubPlugin(PluginConfig(name="stub", priority=1))
        registry.register(low)
        registry.register(SecondStubPlugin())    # 10
        assert [p.name for p in registry.get_all()] == ["stub", "stub_two"]

    def test_execution_follows_priority_order(self, manager):
        manager.register(StubPlugin())           # 50
        manager.register(SecondStubPlugin())     # 10
        results, _ = manager.discover_all(
            industry="Roofing", location="Dallas Texas", limit=10
        )
        assert [r.plugin_name for r in results] == ["stub_two", "stub"]

    def test_capability_filter_preserves_priority_order(self, registry):
        registry.register(StubPlugin())          # COMPANY_DISCOVERY, 50
        registry.register(SecondStubPlugin())    # COMPANY_DISCOVERY, 10
        registry.register(UnhealthyPlugin())     # NEWS_DISCOVERY, 30

        matched = registry.get_by_capability(PluginCapability.COMPANY_DISCOVERY)
        assert [p.name for p in matched] == ["stub_two", "stub"]


# ----------------------------------------------------------------------
# Capabilities — open by design
# ----------------------------------------------------------------------


class TestCapabilities:

    def test_all_required_capabilities_exist(self):
        expected = {
            "company_discovery",
            "leadership_discovery",
            "email_discovery",
            "phone_discovery",
            "website_discovery",
            "social_discovery",
            "project_discovery",
            "bid_discovery",
            "permit_discovery",
            "news_discovery",
        }
        assert expected <= {c.value for c in PluginCapability}

    def test_normalize_accepts_enum_and_string(self):
        assert (
            normalize_capability(PluginCapability.EMAIL_DISCOVERY)
            == "email_discovery"
        )
        assert normalize_capability("  Email_Discovery ") == "email_discovery"

    def test_normalize_rejects_other_types(self):
        with pytest.raises(TypeError):
            normalize_capability(42)

    def test_supports_accepts_enum_or_string(self):
        plugin = StubPlugin()
        assert plugin.supports(PluginCapability.COMPANY_DISCOVERY) is True
        assert plugin.supports("company_discovery") is True
        assert plugin.supports(PluginCapability.BID_DISCOVERY) is False

    def test_unknown_capability_needs_no_framework_change(self, registry):
        """A raw string capability filters exactly like a declared member."""
        plugin = CustomCapabilityPlugin()
        registry.register(plugin)

        assert plugin.supports("patent_discovery") is True
        matched = registry.get_by_capability("patent_discovery")
        assert [p.name for p in matched] == ["custom_cap"]
        assert "patent_discovery" in registry.capabilities()

    def test_custom_capability_is_executable(self, manager):
        manager.register(CustomCapabilityPlugin())
        results, diagnostics = manager.discover_all(
            industry="Roofing",
            location="Dallas",
            limit=5,
            capability="patent_discovery",
        )
        assert [r.plugin_name for r in results] == ["custom_cap"]
        assert diagnostics["capability_filter"] == "patent_discovery"

    def test_supports_any_and_all(self):
        plugin = StubPlugin()
        assert plugin.supports_any(
            [PluginCapability.COMPANY_DISCOVERY, PluginCapability.BID_DISCOVERY]
        ) is True
        assert plugin.supports_all(
            [PluginCapability.COMPANY_DISCOVERY, PluginCapability.BID_DISCOVERY]
        ) is False

    def test_describe_emits_plain_strings(self):
        assert StubPlugin().describe()["capabilities"] == ["company_discovery"]


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


class TestPluginConfig:

    def test_all_required_fields_have_defaults(self):
        config = PluginConfig(name="stub")
        assert config.enabled is True
        assert config.priority == 100
        assert config.weight == 1.0
        assert config.retry_count >= 0
        assert config.timeout > 0
        assert config.rate_limit >= 0
        assert config.cache_ttl >= 0
        assert config.max_results > 0
        assert config.supports_parallel is False
        assert config.supports_batch is False
        assert config.options == {}

    def test_name_is_required(self):
        with pytest.raises(ValueError):
            PluginConfig(name="  ")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"timeout": 0},
            {"max_results": -1},
            {"retry_count": -1},
            {"rate_limit": -1},
            {"cache_ttl": -1},
            {"weight": -1},
        ],
    )
    def test_invalid_values_are_rejected(self, kwargs):
        with pytest.raises(ValueError):
            PluginConfig(name="stub", **kwargs)

    def test_legacy_timeout_seconds_keyword_still_works(self):
        config = PluginConfig(name="stub", timeout_seconds=12.5)
        assert config.timeout == 12.5

    def test_legacy_timeout_seconds_attribute_tracks_timeout(self):
        config = PluginConfig(name="stub", timeout=7.0)
        assert config.timeout_seconds == 7.0
        config.timeout = 9.0
        assert config.timeout_seconds == 9.0

    def test_to_dict_emits_both_timeout_spellings(self):
        data = PluginConfig(name="stub", timeout=8.0).to_dict()
        assert data["timeout"] == 8.0
        assert data["timeout_seconds"] == 8.0

    def test_from_dict_accepts_either_timeout_spelling(self):
        assert PluginConfig.from_dict(
            {"name": "a", "timeout_seconds": 3.0}
        ).timeout == 3.0
        assert PluginConfig.from_dict({"name": "a", "timeout": 4.0}).timeout == 4.0

    def test_from_dict_collects_unknown_keys_into_options(self):
        config = PluginConfig.from_dict({"name": "a", "api_key": "xyz"})
        assert config.get("api_key") == "xyz"

    def test_round_trip_through_dict(self):
        original = PluginConfig(
            name="stub",
            weight=2.5,
            retry_count=5,
            timeout=11.0,
            rate_limit=3.0,
            cache_ttl=60,
            supports_parallel=True,
            supports_batch=True,
            options={"endpoint": "https://x.test"},
        )
        restored = PluginConfig.from_dict(original.to_dict())
        assert restored == original


# ----------------------------------------------------------------------
# Metrics update
# ----------------------------------------------------------------------


class TestMetricsUpdate:

    def test_record_success(self):
        metrics = PluginMetrics(plugin_name="stub")
        metrics.record(status=SourceStatus.SUCCESS, companies=3, latency_ms=100.0)
        assert metrics.total_runs == 1
        assert metrics.success_count == 1
        assert metrics.companies_found == 3
        assert metrics.last_status == SourceStatus.SUCCESS
        assert metrics.last_run is not None
        assert metrics.last_success is not None
        assert metrics.last_error is None

    def test_record_each_status_hits_its_own_counter(self):
        metrics = PluginMetrics(plugin_name="stub")
        metrics.record(status=SourceStatus.SUCCESS)
        metrics.record(status=SourceStatus.EMPTY)
        metrics.record(status=SourceStatus.UNAVAILABLE)
        metrics.record(status=SourceStatus.ERROR)

        assert metrics.total_runs == 4
        assert metrics.success_count == 1
        assert metrics.empty_runs == 1
        assert metrics.unavailable_count == 1
        assert metrics.error_count == 1
        assert metrics.failure_count == 2

    def test_last_success_and_last_error_are_independent(self):
        metrics = PluginMetrics(plugin_name="stub")
        metrics.record(status=SourceStatus.SUCCESS, timestamp=100.0)
        metrics.record(status=SourceStatus.ERROR, timestamp=200.0)
        assert metrics.last_success == 100.0
        assert metrics.last_error == 200.0
        assert metrics.last_run == 200.0

    def test_duplicates_removed_accumulates(self):
        metrics = PluginMetrics(plugin_name="stub")
        metrics.record(status=SourceStatus.SUCCESS, companies=3, duplicates=2)
        metrics.record(status=SourceStatus.SUCCESS, companies=1, duplicates=1)
        assert metrics.duplicates_removed == 3
        assert metrics.companies_found == 4
        assert metrics.deduplication_rate == pytest.approx(3 / 7)

    def test_average_latency_and_rates(self):
        metrics = PluginMetrics(plugin_name="stub")
        metrics.record(status=SourceStatus.SUCCESS, companies=4, latency_ms=100.0)
        metrics.record(status=SourceStatus.EMPTY, companies=0, latency_ms=200.0)

        assert metrics.success_rate == 0.5
        assert metrics.average_latency == 150.0
        assert metrics.avg_companies_per_run == 2.0

    def test_rates_are_zero_before_any_run(self):
        metrics = PluginMetrics(plugin_name="stub")
        assert metrics.success_rate == 0.0
        assert metrics.average_latency == 0.0
        assert metrics.avg_companies_per_run == 0.0
        assert metrics.deduplication_rate == 0.0

    def test_reset_zeroes_counters(self):
        metrics = PluginMetrics(plugin_name="stub")
        metrics.record(status=SourceStatus.SUCCESS, companies=2, duplicates=1)
        metrics.reset()
        assert metrics.total_runs == 0
        assert metrics.companies_found == 0
        assert metrics.duplicates_removed == 0
        assert metrics.last_status is None
        assert metrics.last_success is None
        assert metrics.plugin_name == "stub"

    def test_to_dict_exposes_every_required_key(self):
        data = PluginMetrics(plugin_name="stub").to_dict()
        required = {
            "success_count",
            "error_count",
            "empty_runs",
            "companies_found",
            "duplicates_removed",
            "average_latency",
            "last_run",
            "last_success",
            "last_error",
        }
        assert required <= set(data)

    def test_manager_updates_metrics_on_success(self, manager):
        manager.register(StubPlugin())
        manager.discover_all(industry="Roofing", location="Dallas Texas", limit=10)

        metrics = manager.get_metrics("stub")
        assert metrics.total_runs == 1
        assert metrics.success_count == 1
        assert metrics.companies_found == 1

    def test_manager_records_plugin_reported_duplicates(self, manager):
        manager.register(StubPlugin(metadata={"duplicates_removed": 4}))
        results, diagnostics = manager.discover_all(
            industry="Roofing", location="Dallas", limit=10
        )
        assert results[0].duplicates_removed == 4
        assert diagnostics["duplicates_removed"] == 4
        assert manager.get_metrics("stub").duplicates_removed == 4

    def test_malformed_duplicate_count_is_tolerated(self, manager):
        manager.register(StubPlugin(metadata={"duplicates_removed": "lots"}))
        results, _ = manager.discover_all(
            industry="Roofing", location="Dallas", limit=10
        )
        assert results[0].duplicates_removed == 0
        assert results[0].status == SourceStatus.SUCCESS

    def test_manager_records_error_when_plugin_raises(self, manager):
        manager.register(RaisingPlugin())
        results, _ = manager.discover_all(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        assert results[0].status == SourceStatus.ERROR
        assert "boom" in results[0].error
        metrics = manager.get_metrics("raiser")
        assert metrics.error_count == 1
        assert metrics.success_count == 0
        assert metrics.last_error is not None

    def test_one_failing_plugin_does_not_stop_the_others(self, manager):
        manager.register(RaisingPlugin())        # priority 20, raises
        manager.register(StubPlugin())           # priority 50, succeeds
        results, diagnostics = manager.discover_all(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        assert [r.plugin_name for r in results] == ["raiser", "stub"]
        assert results[1].status == SourceStatus.SUCCESS
        assert diagnostics["plugins_executed"] == 2

    def test_metrics_accumulate_across_runs(self, manager):
        manager.register(StubPlugin())
        manager.discover_all(industry="Roofing", location="Dallas", limit=10)
        manager.discover_all(industry="Roofing", location="Dallas", limit=10)
        assert manager.get_metrics("stub").total_runs == 2

    def test_reset_metrics_clears_all_plugins(self, manager):
        manager.register(StubPlugin())
        manager.discover_all(industry="Roofing", location="Dallas", limit=10)
        manager.reset_metrics()
        assert manager.get_metrics("stub").total_runs == 0


# ----------------------------------------------------------------------
# Health update
# ----------------------------------------------------------------------


class TestHealthUpdate:

    def test_starts_unknown(self):
        health = PluginHealth(plugin_name="stub")
        assert health.state == PluginState.UNKNOWN
        assert health.status == SourceHealth.UNKNOWN
        assert health.is_healthy is False

    def test_success_marks_healthy(self):
        health = PluginHealth(plugin_name="stub")
        health.record_status(SourceStatus.SUCCESS)
        assert health.state == PluginState.HEALTHY
        assert health.status == SourceHealth.HEALTHY
        assert health.consecutive_failures == 0
        assert health.last_checked_at is not None

    def test_empty_is_healthy(self):
        health = PluginHealth(plugin_name="stub")
        health.record_status(SourceStatus.EMPTY)
        assert health.state == PluginState.HEALTHY

    def test_first_error_is_degraded_not_unavailable(self):
        health = PluginHealth(plugin_name="stub")
        health.record_status(SourceStatus.ERROR, message="boom")
        assert health.state == PluginState.DEGRADED
        assert health.is_degraded is True
        assert health.is_operational is True
        assert health.consecutive_failures == 1
        assert health.message == "boom"

    def test_degraded_still_reads_as_unhealthy_for_phase_1(self):
        """The legacy SourceHealth verdict must not change."""
        health = PluginHealth(plugin_name="stub")
        health.record_status(SourceStatus.ERROR)
        assert health.state == PluginState.DEGRADED
        assert health.status == SourceHealth.UNHEALTHY
        assert health.is_healthy is False

    def test_repeated_errors_escalate_to_unavailable(self):
        health = PluginHealth(plugin_name="stub")
        for _ in range(health.unavailable_threshold):
            health.record_status(SourceStatus.ERROR)
        assert health.state == PluginState.UNAVAILABLE
        assert health.status == SourceHealth.UNHEALTHY
        assert health.is_operational is False

    def test_unavailable_status_skips_the_ladder(self):
        health = PluginHealth(plugin_name="stub")
        health.record_status(SourceStatus.UNAVAILABLE)
        assert health.state == PluginState.UNAVAILABLE
        assert health.consecutive_failures == 1

    def test_consecutive_failures_accumulate_then_reset(self):
        health = PluginHealth(plugin_name="stub")
        health.record_status(SourceStatus.ERROR)
        health.record_status(SourceStatus.UNAVAILABLE)
        assert health.consecutive_failures == 2

        health.record_status(SourceStatus.SUCCESS)
        assert health.consecutive_failures == 0
        assert health.state == PluginState.HEALTHY

    def test_every_required_state_maps_to_a_source_health(self):
        for state in (
            PluginState.HEALTHY,
            PluginState.DEGRADED,
            PluginState.UNAVAILABLE,
            PluginState.DISABLED,
            PluginState.MAINTENANCE,
        ):
            health = PluginHealth(plugin_name="stub", state=state)
            assert isinstance(health.status, SourceHealth)

    def test_disabled_and_maintenance_read_as_unknown(self):
        health = PluginHealth(plugin_name="stub")
        health.mark_disabled()
        assert health.state == PluginState.DISABLED
        assert health.status == SourceHealth.UNKNOWN
        assert health.consecutive_failures == 0

        health.mark_maintenance()
        assert health.state == PluginState.MAINTENANCE
        assert health.status == SourceHealth.UNKNOWN

    def test_record_check_passing(self):
        health = PluginHealth(plugin_name="stub")
        health.record_check(healthy=True, details={"probe": "ok"})
        assert health.state == PluginState.HEALTHY
        assert health.details == {"probe": "ok"}

    def test_record_check_failing(self):
        health = PluginHealth(plugin_name="stub")
        health.record_check(healthy=False, message="unreachable")
        assert health.state == PluginState.DEGRADED
        assert health.status == SourceHealth.UNHEALTHY
        assert health.consecutive_failures == 1

    def test_record_check_honours_explicit_state(self):
        health = PluginHealth(plugin_name="stub")
        health.record_check(healthy=False, state="maintenance")
        assert health.state == PluginState.MAINTENANCE
        assert health.consecutive_failures == 0

    def test_unrecognized_state_becomes_unknown(self):
        health = PluginHealth(plugin_name="stub")
        health.record_check(healthy=False, state="banana")
        assert health.state == PluginState.UNKNOWN

    def test_mark_unknown_does_not_count_a_failure(self):
        health = PluginHealth(plugin_name="stub")
        health.mark_unknown("plugin disabled")
        assert health.state == PluginState.UNKNOWN
        assert health.status == SourceHealth.UNKNOWN
        assert health.consecutive_failures == 0

    def test_reset_returns_to_initial_state(self):
        health = PluginHealth(plugin_name="stub")
        health.record_status(SourceStatus.ERROR)
        health.reset()
        assert health.state == PluginState.UNKNOWN
        assert health.consecutive_failures == 0
        assert health.last_checked_at is None

    def test_to_dict_exposes_state_and_legacy_status(self):
        data = PluginHealth(plugin_name="stub").to_dict()
        assert data["state"] == "unknown"
        assert data["status"] == "unknown"

    def test_manager_updates_health_after_discovery(self, manager):
        manager.register(StubPlugin())
        manager.register(RaisingPlugin())
        manager.discover_all(industry="Roofing", location="Dallas", limit=10)

        assert manager.get_health("stub").state == PluginState.HEALTHY
        assert manager.get_health("raiser").state == PluginState.DEGRADED
        assert manager.get_health("raiser").status == SourceHealth.UNHEALTHY

    def test_check_health_uses_plugin_report(self, manager):
        manager.register(StubPlugin())
        manager.register(UnhealthyPlugin())

        health = asyncio.run(manager.check_health())
        assert health["stub"].state == PluginState.HEALTHY
        assert health["unhealthy"].status == SourceHealth.UNHEALTHY

    def test_check_health_honours_plugin_reported_state(self, manager):
        manager.register(MaintenancePlugin())
        health = asyncio.run(manager.check_health())
        assert health["maintenance"].state == PluginState.MAINTENANCE
        assert health["maintenance"].status == SourceHealth.UNKNOWN

    def test_check_health_marks_disabled_plugin_disabled(self, manager):
        manager.register(StubPlugin())
        manager.registry.disable("stub")

        health = asyncio.run(manager.check_health())
        assert health["stub"].state == PluginState.DISABLED
        assert health["stub"].status == SourceHealth.UNKNOWN
        assert health["stub"].consecutive_failures == 0

    def test_report_includes_plugins_metrics_and_health(self, manager):
        manager.register(StubPlugin())
        manager.discover_all(industry="Roofing", location="Dallas", limit=10)
        report = manager.report()
        assert report["plugins"][0]["name"] == "stub"
        assert report["metrics"]["stub"]["total_runs"] == 1
        assert report["health"]["stub"]["state"] == "healthy"
