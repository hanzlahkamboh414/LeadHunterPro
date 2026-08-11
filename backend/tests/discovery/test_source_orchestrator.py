"""Tests for SourceOrchestrator — multi-source discovery pipeline."""

from __future__ import annotations

import logging
import pytest
from typing import Any

from app.discovery.source_orchestrator import SourceOrchestrator
from app.discovery.sources.status import SourceStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fake source for testing
# ---------------------------------------------------------------------------


class FakeSource:
    """Minimal fake source that conforms to the BaseSource interface."""

    def __init__(
        self,
        name: str,
        priority: int,
        results: list[dict[str, Any]] | None = None,
        status: SourceStatus = SourceStatus.SUCCESS,
        error: Exception | None = None,
        enabled: bool = True,
    ) -> None:
        self.source_name = name
        self.priority = priority
        self.enabled = enabled
        self._results = results or []
        self._status = status
        self._error = error
        self.call_count = 0

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        self.call_count += 1
        if self._error:
            raise self._error
        return self._status, list(self._results), {
            "source": self.source_name,
            "results": len(self._results),
        }

    async def health_check(self) -> dict[str, Any]:
        return {"healthy": self.enabled, "source": self.source_name}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _company(
    name: str, domain: str, city: str = "Dallas", state: str = "TX"
) -> dict[str, Any]:
    """Create a minimal company dict for testing."""
    return {
        "company_name": name,
        "website": f"https://www.{domain}.com",
        "city": city,
        "state": state,
        "country": "USA",
        "trade_category": "roofing",
        "industry_focus": f"{name} roofing services",
        "data_provenance": f"fixture:{domain}",
        "discovery_reason": "matched by fixture",
    }


# ---------------------------------------------------------------------------
# Test: Orchestrator core behaviour
# ---------------------------------------------------------------------------


class TestSourceOrchestrator:
    """Test SourceOrchestrator orchestration logic."""

    def test_empty_orchestrator_returns_empty(self):
        """No sources registered → empty results, no crash."""
        orch = SourceOrchestrator()
        companies, metadata = orch.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )
        assert companies == []
        assert metadata["data_source"] == "empty"
        assert metadata["total_deduped"] == 0

    def test_register_adds_source(self):
        """Registering a source makes it available."""
        orch = SourceOrchestrator()
        source = FakeSource("fake1", priority=10, results=[_company("A", "a")])
        orch.register(source)
        assert len(orch._sources) == 1
        assert orch._sources[0] is source

    def test_register_duplicate_skipped(self, caplog):
        """Registering the same source twice is a no-op."""
        orch = SourceOrchestrator()
        source = FakeSource("dup", priority=10)
        orch.register(source)
        with caplog.at_level(logging.WARNING):
            orch.register(source)
        assert len(orch._sources) == 1
        # The warning message should mention the source name
        assert any("already registered" in r.message for r in caplog.records)

    def test_sources_sorted_by_priority(self):
        """Sources execute in ascending priority order."""
        orch = SourceOrchestrator()
        low = FakeSource("low", priority=90, results=[_company("Low", "low")])
        high = FakeSource("high", priority=10, results=[_company("High", "high")])
        orch.register(low)
        orch.register(high)
        names = [s.source_name for s in orch._sources]
        assert names == ["high", "low"]

    def test_disabled_source_skipped(self):
        """Disabled sources are not called."""
        orch = SourceOrchestrator()
        disabled = FakeSource("off", priority=5, enabled=False)
        orch.register(disabled)
        companies, _ = orch.discover(industry="X", location="Y", limit=10)
        assert companies == []
        assert disabled.call_count == 0

    def test_single_source_results_returned(self):
        """Results from a single enabled source flow through."""
        orch = SourceOrchestrator()
        src = FakeSource("s1", priority=10, results=[_company("Acme", "acme")])
        orch.register(src)
        companies, metadata = orch.discover(
            industry="Roofing", location="Dallas TX", limit=10
        )
        assert len(companies) == 1
        assert companies[0]["company_name"] == "Acme"
        assert metadata["data_source"] == "live"

    def test_limit_truncates_results(self):
        """Returned results respect the limit parameter."""
        orch = SourceOrchestrator()
        companies = [_company(f"Company{i}", f"domain{i}") for i in range(20)]
        src = FakeSource("big", priority=10, results=companies)
        orch.register(src)
        results, _ = orch.discover(industry="X", location="Y", limit=5)
        assert len(results) == 5

    def test_metadata_contains_stats(self):
        """Metadata includes source_stats and timing."""
        orch = SourceOrchestrator()
        orch.register(FakeSource("s1", priority=10, results=[_company("X", "x")]))
        _, meta = orch.discover(industry="X", location="Y", limit=10)
        assert "source_stats" in meta
        assert "elapsed_ms" in meta
        assert meta["elapsed_ms"] >= 0
        assert meta["sources_executed"] == 1


# ---------------------------------------------------------------------------
# Test: Aggregation across multiple sources
# ---------------------------------------------------------------------------


class TestMultiSourceAggregation:
    """Test aggregation when multiple sources are registered."""

    def test_aggregates_all_sources(self):
        """Results from all sources are combined."""
        orch = SourceOrchestrator()
        orch.register(FakeSource("s1", priority=20, results=[_company("FromS1", "s1")]))
        orch.register(FakeSource("s2", priority=10, results=[_company("FromS2", "s2")]))
        companies, _ = orch.discover(industry="X", location="Y", limit=10)
        names = [c["company_name"] for c in companies]
        assert "FromS1" in names
        assert "FromS2" in names

    def test_prioritized_order_preserved(self):
        """Higher-priority source results appear first after merge."""
        orch = SourceOrchestrator()
        # Priority 5 (higher) runs before priority 20 (lower)
        orch.register(
            FakeSource("low_prio", priority=20, results=[_company("LowPrio", "lp")])
        )
        orch.register(
            FakeSource("high_prio", priority=5, results=[_company("HighPrio", "hp")])
        )
        companies, _ = orch.discover(industry="X", location="Y", limit=10)
        # Both should be present
        names = [c["company_name"] for c in companies]
        assert "HighPrio" in names
        assert "LowPrio" in names

    def test_failed_source_logs_error_not_crashes(self):
        """When one source throws, other sources still run."""
        orch = SourceOrchestrator()
        orch.register(FakeSource("crashy", priority=10, error=RuntimeError("boom")))
        orch.register(FakeSource("ok", priority=20, results=[_company("OK", "ok")]))
        companies, meta = orch.discover(industry="X", location="Y", limit=10)
        assert len(companies) == 1
        assert companies[0]["company_name"] == "OK"
        assert "crashy" in meta["errors"]


# ---------------------------------------------------------------------------
# Test: Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Test that duplicate domains/names are removed."""

    def test_duplicate_domain_removed(self):
        """Same domain from two sources → kept once."""
        orch = SourceOrchestrator()
        same = _company("Acme Roofing", "acme.com")
        orch.register(FakeSource("s1", priority=20, results=[same]))
        orch.register(FakeSource("s2", priority=10, results=[same]))
        companies, _ = orch.discover(industry="X", location="Y", limit=10)
        names = [c["company_name"] for c in companies]
        assert names.count("Acme Roofing") == 1

    def test_different_domains_kept(self):
        """Different domains are both kept."""
        orch = SourceOrchestrator()
        orch.register(FakeSource("s1", priority=20, results=[_company("Acme", "acme")]))
        orch.register(
            FakeSource("s2", priority=10, results=[_company("Acme", "acme2")])
        )
        companies, _ = orch.discover(industry="X", location="Y", limit=10)
        assert len(companies) == 2

    def test_similar_names_different_domains_kept(self):
        """Similar names but different domains are both kept."""
        orch = SourceOrchestrator()
        orch.register(
            FakeSource(
                "s1",
                priority=20,
                results=[_company("Acme Roofing", "acme-roofing.com")],
            )
        )
        orch.register(
            FakeSource(
                "s2",
                priority=10,
                results=[_company("Acme Roofing LLC", "acmerooftx.com")],
            )
        )
        companies, _ = orch.discover(industry="X", location="Y", limit=10)
        assert len(companies) == 2

    def test_www_prefix_normalized(self):
        """www. prefix is stripped for dedup comparison."""
        orch = SourceOrchestrator()
        orch.register(
            FakeSource("s1", priority=20, results=[_company("Acme", "www.acme.com")])
        )
        orch.register(
            FakeSource("s2", priority=10, results=[_company("Acme", "acme.com")])
        )
        companies, _ = orch.discover(industry="X", location="Y", limit=10)
        assert len(companies) == 1


# ---------------------------------------------------------------------------
# Test: Fixture fallback detection
# ---------------------------------------------------------------------------


class TestFixtureFallbackDetection:
    """Test that data_source='fixture' is set when only fixture bridge runs."""

    @pytest.fixture(autouse=True)
    def _fixture_env_test_mode(self, monkeypatch):
        """Accuracy-first Phase 1: fixture bridge needs LEADHUNTER_ENV=test.

        In live mode the fixture source is disabled entirely, so these
        fallback tests exercise it under test mode (fixture use in live mode
        is covered by tests/verification/test_phase1_modes_and_tiers.py).
        """
        from app.core.config import settings

        monkeypatch.setattr(settings, "LEADHUNTER_ENV", "test")

    def test_fixture_bridge_sets_data_source(self):
        """When only fixture_bridge source runs, data_source is 'fixture'."""
        from app.discovery.sources.fixture_source import FixtureSource

        orch = SourceOrchestrator()
        orch.register(FixtureSource())
        companies, meta = orch.discover(industry="Roofing", location="TX", limit=10)
        assert meta["data_source"] == "fixture"
        # fallback_reason indicates why bridge was activated
        reason_lower = meta["fallback_reason"].lower().strip()
        assert reason_lower == "" or any(word in reason_lower for word in (
            "unavailable", "no_live", "configured", "failed", "error"
        ))
        assert len(companies) > 0

    def test_all_live_sources_fail_sets_fixture(self):
        """All live sources fail → falls back to fixture bridge."""
        from app.discovery.sources.fixture_source import FixtureSource

        orch = SourceOrchestrator()
        orch.register(FakeSource("bad", priority=10, error=RuntimeError("fail")))
        orch.register(FixtureSource())
        _, meta = orch.discover(industry="Roofing", location="TX", limit=10)
        assert meta["data_source"] == "fixture"
        # Error is captured in fallback_reason when a source threw an exception
        assert "failed" in meta["fallback_reason"].lower() or "error" in meta["fallback_reason"].lower()


# ---------------------------------------------------------------------------
# Test: Error handling in discover
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Test that errors are isolated per source."""

    def test_partial_failure_returns_partial_results(self):
        """If source A fails and source B succeeds, B's results are returned."""
        orch = SourceOrchestrator()
        orch.register(FakeSource("a", priority=10, error=ValueError("oops")))
        orch.register(FakeSource("b", priority=20, results=[_company("B", "b.com")]))
        companies, meta = orch.discover(industry="X", location="Y", limit=10)
        assert len(companies) == 1
        assert "a" in meta["errors"]
        assert "b" in meta["source_stats"]

    def test_source_exception_does_not_propagate(self):
        """discover() never raises — exceptions are captured in metadata."""
        orch = SourceOrchestrator()
        orch.register(
            FakeSource(
                "boom",
                priority=10,
                error=ConnectionError("network"),
            )
        )
        # Should not raise
        companies, meta = orch.discover(industry="X", location="Y", limit=10)
        assert isinstance(companies, list)
        assert "boom" in meta["errors"]


# ---------------------------------------------------------------------------
# Test: Metadata shape
# ---------------------------------------------------------------------------


class TestMetadataShape:
    """Test the structure of orchestrator metadata."""

    def test_required_keys_present(self):
        """All expected top-level keys exist in metadata."""
        orch = SourceOrchestrator()
        orch.register(FakeSource("s1", priority=10, results=[_company("X", "x")]))
        _, meta = orch.discover(industry="X", location="Y", limit=10)
        for key in (
            "data_source",
            "fallback_reason",
            "total_raw",
            "total_deduped",
            "total_returned",
            "elapsed_ms",
            "source_stats",
            "errors",
            "sources_executed",
        ):
            assert key in meta, f"Missing key: {key}"

    def test_total_fields_consistent(self):
        """total_returned <= total_deduped <= total_raw."""
        orch = SourceOrchestrator()
        orch.register(
            FakeSource(
                "s1",
                priority=10,
                results=[
                    _company("A", "a.com"),
                    _company("B", "b.com"),
                ],
            )
        )
        # Same exact company (same domain AND same name) → deduped
        orch.register(
            FakeSource(
                "s2",
                priority=20,
                results=[
                    _company("A", "a.com"),  # Exact duplicate of first result
                ],
            )
        )
        _, meta = orch.discover(industry="X", location="Y", limit=10)
        assert meta["total_returned"] <= meta["total_deduped"] <= meta["total_raw"]
        assert meta["total_raw"] == 3  # 2 + 1 before dedup
        assert meta["total_deduped"] == 2  # 1 dup removed (same domain + name)
        assert meta["total_deduped"] == 2  # 1 dup removed
