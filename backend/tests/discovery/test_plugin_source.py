"""Tests for PluginSource — plugin framework integrated into the pipeline.

Phase 2.2 covers exactly one thing: the plugin framework reaching the
existing pipeline through :class:`SourceOrchestrator` without changing
what that pipeline already does.

The two properties that matter here are:

1. **Backward compatibility.** With an empty registry the orchestrator
   must produce byte-identical metadata to a run with no plugin source
   at all.
2. **Honest status.** A plugin source must never make the pipeline
   believe live discovery succeeded when it produced nothing.

Every plugin in this file is a stub. No real provider is involved.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.discovery.plugins.base_plugin import BaseDiscoveryPlugin, PluginCapability
from app.discovery.plugins.plugin_manager import (
    PluginManager,
    get_manager,
    reset_manager,
)
from app.discovery.plugins.plugin_registry import PluginRegistry
from app.discovery.source_orchestrator import SourceOrchestrator
from app.discovery.sources.plugin_source import (
    DEFAULT_PLUGIN_SOURCE_PRIORITY,
    PluginSource,
    attach_plugin_source,
)
from app.discovery.sources.status import SourceStatus

# ----------------------------------------------------------------------
# Stubs
# ----------------------------------------------------------------------


def _company(name: str, domain: str) -> dict[str, Any]:
    """Create a minimal company dict."""
    return {
        "company_name": name,
        "website": f"https://www.{domain}.com",
        "city": "Dallas",
        "state": "TX",
    }


class StubPlugin(BaseDiscoveryPlugin):
    """Plugin that returns whatever it is constructed with."""

    name = "stub_plugin"
    priority = 10
    capabilities = (PluginCapability.COMPANY_DISCOVERY,)

    def __init__(
        self,
        config=None,
        *,
        status: SourceStatus = SourceStatus.SUCCESS,
        companies: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(config)
        self._status = status
        self._companies = (
            companies if companies is not None else [_company("Stub Co", "stub")]
        )
        self.calls = 0

    def discover(self, *, industry, location, limit):
        self.calls += 1
        return self._status, list(self._companies), {"query": industry}


class SecondStubPlugin(StubPlugin):
    """A differently-named stub for multi-plugin tests."""

    name = "stub_plugin_two"
    priority = 20


class RaisingPlugin(BaseDiscoveryPlugin):
    """Plugin whose discover() always raises."""

    name = "raiser"
    priority = 30
    capabilities = (PluginCapability.COMPANY_DISCOVERY,)

    def discover(self, *, industry, location, limit):
        raise RuntimeError("boom")


class LeadershipPlugin(StubPlugin):
    """Plugin that does NOT declare COMPANY_DISCOVERY."""

    name = "leadership_only"
    priority = 5
    capabilities = (PluginCapability.LEADERSHIP_DISCOVERY,)


class FakeSource:
    """Minimal BaseSource-compatible fake (mirrors the orchestrator tests)."""

    def __init__(
        self,
        name: str,
        priority: int,
        results: list[dict[str, Any]] | None = None,
        status: SourceStatus = SourceStatus.SUCCESS,
        enabled: bool = True,
    ) -> None:
        self.source_name = name
        self.priority = priority
        self.enabled = enabled
        self._results = results or []
        self._status = status

    def discover(self, *, industry, location, limit):
        return self._status, list(self._results), {"source": self.source_name}


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def manager() -> PluginManager:
    """A manager over an isolated registry — never the shared singleton."""
    return PluginManager(registry=PluginRegistry())


@pytest.fixture(autouse=True)
def _clean_shared_state():
    """Keep the process-wide registry and manager out of these tests."""
    PluginRegistry.reset_instance()
    reset_manager()
    yield
    PluginRegistry.reset_instance()
    reset_manager()


def _discover(source: PluginSource):
    """Run a discover() call with standard arguments."""
    return source.discover(industry="Roofing", location="Dallas Texas", limit=10)


# ----------------------------------------------------------------------
# Backward compatibility — the headline requirement of Phase 2.2
# ----------------------------------------------------------------------


class TestBackwardCompatibility:
    """No plugins registered => the pipeline behaves exactly as before."""

    def test_attach_is_noop_when_registry_empty(self, manager):
        """Nothing is registered when there is no plugin to run."""
        orch = SourceOrchestrator()
        attached = attach_plugin_source(orch, manager=manager)
        assert attached is None
        assert orch._sources == []

    def test_metadata_identical_with_and_without_attach(self, manager):
        """An empty registry leaves orchestrator metadata untouched."""
        baseline_orch = SourceOrchestrator()
        baseline_orch.register(FakeSource("search", 50, status=SourceStatus.EMPTY))
        baseline, baseline_meta = baseline_orch.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        plugin_orch = SourceOrchestrator()
        attach_plugin_source(plugin_orch, manager=manager)
        plugin_orch.register(FakeSource("search", 50, status=SourceStatus.EMPTY))
        companies, meta = plugin_orch.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        assert companies == baseline
        # Every decision-bearing field must be identical. elapsed_ms is
        # wall-clock and legitimately differs.
        for key in (
            "data_source",
            "bridge_mode",
            "fallback_reason",
            "total_raw",
            "total_deduped",
            "total_returned",
            "sources_executed",
            "errors",
        ):
            assert meta[key] == baseline_meta[key], key
        assert set(meta["source_stats"]) == set(baseline_meta["source_stats"])

    def test_attach_skips_when_only_disabled_plugins(self, manager):
        """A registered-but-disabled plugin is not a reason to attach."""
        plugin = StubPlugin()
        manager.register(plugin)
        manager.registry.disable(plugin.name)

        orch = SourceOrchestrator()
        assert attach_plugin_source(orch, manager=manager) is None
        assert orch._sources == []

    def test_attach_skips_when_capability_unmatched(self, manager):
        """Capability filter excludes every plugin => nothing attached."""
        manager.register(LeadershipPlugin())
        orch = SourceOrchestrator()
        attached = attach_plugin_source(
            orch, manager=manager, capability=PluginCapability.COMPANY_DISCOVERY
        )
        assert attached is None
        assert orch._sources == []

    def test_noop_attach_is_logged_not_silent(self, manager, caplog):
        """CLAUDE.md §5: 'no plugins ran' must never be silent."""
        orch = SourceOrchestrator()
        with caplog.at_level(logging.INFO):
            attach_plugin_source(orch, manager=manager)
        assert any(
            "PluginSource NOT attached" in r.message for r in caplog.records
        )


# ----------------------------------------------------------------------
# Attachment
# ----------------------------------------------------------------------


class TestAttach:
    """attach_plugin_source wiring behaviour."""

    def test_attaches_when_plugin_registered(self, manager):
        manager.register(StubPlugin())
        orch = SourceOrchestrator()
        source = attach_plugin_source(orch, manager=manager)
        assert isinstance(source, PluginSource)
        assert orch._sources == [source]

    def test_default_priority_runs_before_search_and_fixture(self, manager):
        """Plugins are real sources; search APIs are optional infrastructure."""
        manager.register(StubPlugin())
        orch = SourceOrchestrator()
        orch.register(FakeSource("fixture_bridge", 999))
        orch.register(FakeSource("search_providers", 50))
        source = attach_plugin_source(orch, manager=manager)
        assert source.priority == DEFAULT_PLUGIN_SOURCE_PRIORITY
        assert [s.source_name for s in orch._sources] == [
            "discovery_plugins",
            "search_providers",
            "fixture_bridge",
        ]

    def test_priority_override(self, manager):
        manager.register(StubPlugin())
        orch = SourceOrchestrator()
        source = attach_plugin_source(orch, manager=manager, priority=5)
        assert source.priority == 5

    def test_uses_shared_manager_by_default(self):
        """Omitting `manager` binds to the process-wide shared manager."""
        get_manager().register(StubPlugin())
        orch = SourceOrchestrator()
        source = attach_plugin_source(orch)
        assert source is not None
        assert source.manager is get_manager()


# ----------------------------------------------------------------------
# discover() — status aggregation
# ----------------------------------------------------------------------


class TestDiscoverStatus:
    """PluginSource must report an honest, aggregated status."""

    def test_success_returns_companies(self, manager):
        manager.register(StubPlugin())
        status, companies, meta = _discover(PluginSource(manager=manager))
        assert status == SourceStatus.SUCCESS
        assert len(companies) == 1
        assert meta["plugins_executed"] == 1
        assert meta["source"] == "discovery_plugins"

    def test_no_plugins_returns_empty_not_error(self, manager):
        """An empty registry is not a failure of this source."""
        status, companies, meta = _discover(PluginSource(manager=manager))
        assert status == SourceStatus.EMPTY
        assert companies == []
        assert meta["selection_reason"] == "no_plugins_registered"

    def test_all_empty_returns_empty(self, manager):
        manager.register(StubPlugin(status=SourceStatus.EMPTY, companies=[]))
        status, companies, _ = _discover(PluginSource(manager=manager))
        assert status == SourceStatus.EMPTY
        assert companies == []

    def test_raising_plugin_becomes_error_not_exception(self, manager):
        """A broken plugin is isolated; the source still returns a tuple."""
        manager.register(RaisingPlugin())
        status, companies, meta = _discover(PluginSource(manager=manager))
        assert status == SourceStatus.ERROR
        assert companies == []
        assert "raiser" in meta["error"]

    def test_unavailable_plugin_reported_as_unavailable(self, manager):
        manager.register(StubPlugin(status=SourceStatus.UNAVAILABLE, companies=[]))
        status, _, _ = _discover(PluginSource(manager=manager))
        assert status == SourceStatus.UNAVAILABLE

    def test_success_wins_over_sibling_failure(self, manager):
        """One good plugin is enough; the failure stays visible in metadata."""
        manager.register(StubPlugin())
        manager.register(RaisingPlugin())
        status, companies, meta = _discover(PluginSource(manager=manager))
        assert status == SourceStatus.SUCCESS
        assert len(companies) == 1
        assert meta["plugin_stats"]["raiser"]["status"] == "error"

    def test_success_with_no_companies_downgraded_to_empty(self, manager):
        """A fake 'live' success must never reach the orchestrator.

        The orchestrator treats any non-fixture SUCCESS as proof that live
        discovery worked. Passing SUCCESS up with zero companies would set
        data_source="live", return nothing, and skip the fixture bridge.
        """
        manager.register(StubPlugin(status=SourceStatus.SUCCESS, companies=[]))
        status, companies, _ = _discover(PluginSource(manager=manager))
        assert status == SourceStatus.EMPTY
        assert companies == []

    def test_companies_from_non_success_plugin_discarded(self, manager):
        """Data attached to a non-SUCCESS status is a contract violation."""
        manager.register(
            StubPlugin(status=SourceStatus.ERROR, companies=[_company("X", "x")])
        )
        status, companies, _ = _discover(PluginSource(manager=manager))
        assert status == SourceStatus.ERROR
        assert companies == []

    def test_results_not_deduplicated_by_the_source(self, manager):
        """Deduplication belongs to the orchestrator, not here."""
        dupe = _company("Same Co", "same")
        manager.register(StubPlugin(companies=[dupe]))
        manager.register(SecondStubPlugin(companies=[dupe]))
        _, companies, _ = _discover(PluginSource(manager=manager))
        assert len(companies) == 2

    def test_capability_filter_selects_subset(self, manager):
        manager.register(StubPlugin())
        manager.register(LeadershipPlugin())
        source = PluginSource(
            manager=manager, capability=PluginCapability.COMPANY_DISCOVERY
        )
        _, _, meta = _discover(source)
        assert meta["plugins_registered"] == 2
        assert meta["plugins_selected"] == 1
        assert meta["capability_filter"] == "company_discovery"

    def test_manager_failure_is_contained(self, manager, monkeypatch):
        """Even a manager-level explosion must not propagate."""

        def _boom(**_kwargs):
            raise RuntimeError("manager exploded")

        monkeypatch.setattr(manager, "discover_all", _boom)
        status, companies, meta = _discover(PluginSource(manager=manager))
        assert status == SourceStatus.ERROR
        assert companies == []
        assert "manager exploded" in meta["error"]


# ----------------------------------------------------------------------
# End-to-end through the orchestrator
# ----------------------------------------------------------------------


class TestThroughOrchestrator:
    """The framework must work through the real orchestrator."""

    def test_plugin_results_flow_into_pipeline(self, manager):
        manager.register(StubPlugin())
        orch = SourceOrchestrator()
        attach_plugin_source(orch, manager=manager)
        companies, meta = orch.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )
        assert len(companies) == 1
        assert meta["data_source"] == "live"
        assert meta["bridge_mode"] is False
        assert "discovery_plugins" in meta["source_stats"]

    def test_orchestrator_deduplicates_across_plugins(self, manager):
        """Cross-plugin duplicates are removed by the orchestrator."""
        dupe = _company("Same Co", "same")
        manager.register(StubPlugin(companies=[dupe]))
        manager.register(SecondStubPlugin(companies=[dupe]))
        orch = SourceOrchestrator()
        attach_plugin_source(orch, manager=manager)
        companies, meta = orch.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )
        assert meta["total_raw"] == 2
        assert meta["total_deduped"] == 1
        assert len(companies) == 1

    def test_failing_plugin_falls_through_to_fixture(self, manager):
        """A broken plugin must not block the fixture bridge."""
        manager.register(RaisingPlugin())
        orch = SourceOrchestrator()
        attach_plugin_source(orch, manager=manager)
        orch.register(
            FakeSource("fixture_bridge", 999, results=[_company("Fixture", "fx")])
        )
        companies, meta = orch.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )
        assert meta["data_source"] == "fixture"
        assert meta["bridge_mode"] is True
        assert "discovery_plugins" in meta["fallback_reason"]
        assert len(companies) == 1

    def test_health_report_survives_disabled_plugin_source(self, manager, capsys):
        """The §6 diagnostics surface must not crash on a disabled source."""
        manager.register(StubPlugin())
        orch = SourceOrchestrator()
        source = attach_plugin_source(orch, manager=manager)
        source.enabled = False
        orch.register(FakeSource("search_providers", 50, status=SourceStatus.EMPTY))
        companies, meta = orch.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )
        orch.print_health_report(companies, meta)
        out = capsys.readouterr().out
        assert "discovery_plugins" in out
        assert "DISABLED" in out


# ----------------------------------------------------------------------
# Metrics, health, shared manager
# ----------------------------------------------------------------------


class TestMetricsAndHealth:
    """Integration must preserve Phase 2.1 tracking."""

    def test_metrics_recorded_through_the_source(self, manager):
        plugin = StubPlugin()
        manager.register(plugin)
        source = PluginSource(manager=manager)
        _discover(source)
        _discover(source)
        metrics = manager.get_metrics(plugin.name)
        assert metrics is not None
        assert metrics.total_runs == 2

    def test_shared_manager_preserves_metrics_across_pipelines(self):
        """A per-request manager would reset counters on every search."""
        plugin = StubPlugin()
        get_manager().register(plugin)

        for _ in range(2):
            orch = SourceOrchestrator()
            attach_plugin_source(orch)
            orch.discover(industry="Roofing", location="Dallas Texas", limit=10)

        metrics = get_manager().get_metrics(plugin.name)
        assert metrics is not None
        assert metrics.total_runs == 2

    def test_health_check_reports_plugin_health(self, manager):
        manager.register(StubPlugin())
        source = PluginSource(manager=manager)
        report = asyncio.run(source.health_check())
        assert report["healthy"] is True
        assert report["source"] == "discovery_plugins"
        assert report["plugins_registered"] == 1
        assert "stub_plugin" in report["plugin_health"]

    def test_health_check_unhealthy_without_plugins(self, manager):
        """Nothing behind the source means it cannot produce data."""
        report = asyncio.run(PluginSource(manager=manager).health_check())
        assert report["healthy"] is False
        assert report["plugins_registered"] == 0


class TestSharedManager:
    """get_manager() singleton semantics."""

    def test_returns_same_instance(self):
        assert get_manager() is get_manager()

    def test_binds_to_shared_registry(self):
        from app.discovery.plugins.plugin_registry import get_registry

        assert get_manager().registry is get_registry()

    def test_reset_creates_a_fresh_manager(self):
        first = get_manager()
        reset_manager()
        assert get_manager() is not first
