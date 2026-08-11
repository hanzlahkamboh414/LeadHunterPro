"""Accuracy-first Phase 1 — execution modes + source quality tiers.

Proves the accuracy-first gating that the later phases build on:

- live mode COMPLETELY excludes the fixture bridge (accuracy Rule 9);
- demo/test mode permits fixtures, still labeled Tier 4;
- fixture records are explicitly stamped ``source_tier == 4``;
- source tier mapping (Tier 1-4) and the live-tier predicate work;
- the structured verification model defaults to *unknown* / no evidence
  (accuracy Rule 8) and never fills location from a query (Rule 1).
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.discovery.source_orchestrator import SourceOrchestrator
from app.discovery.sources.fixture_source import FixtureSource
from app.discovery.sources.status import SourceStatus
from app.engines.verification.models import (
    FieldEvidence,
    LeadRecord,
    VerificationStatus,
)
from app.engines.verification.source_tiers import (
    SourceTier,
    TIER_LABELS,
    is_live_tier,
    tier_for_source,
    tier_label,
)


class _FakeLiveSource:
    """A minimal live source (does not need to subclass BaseSource)."""

    source_name = "fake_live"
    description = "fake live source for tests"
    priority = 10
    enabled = True

    def discover(self, *, industry, location, limit):  # noqa: ARG002
        return (
            SourceStatus.SUCCESS,
            [{"company_name": "Acme Roofing LLC", "city": "Dallas", "state": "TX"}],
            {},
        )


# ---------------------------------------------------------------------------
# Mode gating: live excludes fixtures, demo/test allows them
# ---------------------------------------------------------------------------


class TestModeGating:
    def test_live_mode_disables_fixture_source(self, monkeypatch):
        """LEADHUNTER_ENV=live → FixtureSource.enabled is False."""
        monkeypatch.setattr(settings, "LEADHUNTER_ENV", "live")
        assert FixtureSource().enabled is False

    @pytest.mark.parametrize("env", ["demo", "test"])
    def test_fixture_enabled_in_demo_and_test_modes(self, monkeypatch, env):
        """LEADHUNTER_ENV=demo/test → FixtureSource.enabled is True."""
        monkeypatch.setattr(settings, "LEADHUNTER_ENV", env)
        assert FixtureSource().enabled is True

    def test_live_mode_orchestrator_skips_fixture(self, monkeypatch):
        """Live mode: fixture registered but NOT executed; live wins."""
        monkeypatch.setattr(settings, "LEADHUNTER_ENV", "live")
        orch = SourceOrchestrator()
        orch.register(_FakeLiveSource())
        orch.register(FixtureSource())
        companies, meta = orch.discover(
            industry="Roofing", location="Dallas Texas", limit=5
        )
        assert meta["data_source"] == "live"
        assert "fixture_bridge" not in meta["source_stats"]
        assert companies  # the live company is returned

    def test_live_mode_fixture_only_yields_empty_not_fixture(self, monkeypatch):
        """Live mode: only a (disabled) fixture registered → 'empty', not
        'fixture' — the bridge must never label a live run."""
        monkeypatch.setattr(settings, "LEADHUNTER_ENV", "live")
        orch = SourceOrchestrator()
        orch.register(FixtureSource())
        companies, meta = orch.discover(
            industry="Roofing", location="Dallas Texas", limit=5
        )
        assert meta["data_source"] == "empty"
        assert "fixture_bridge" not in meta["source_stats"]
        assert companies == []

    @pytest.mark.parametrize("env", ["demo", "test"])
    def test_fixture_flows_in_demo_and_test_modes(self, monkeypatch, env):
        """In demo/test mode the fixture source is executable."""
        monkeypatch.setattr(settings, "LEADHUNTER_ENV", env)
        status, companies, meta = FixtureSource().discover(
            industry="Roofing", location="Dallas Texas", limit=5
        )
        assert status == SourceStatus.SUCCESS
        assert companies
        assert meta["data_source"] == "fixture"


# ---------------------------------------------------------------------------
# Fixture records are explicitly labeled Tier 4
# ---------------------------------------------------------------------------


class TestFixtureTierLabeling:
    def test_fixture_records_labeled_tier4(self, monkeypatch):
        """Every fixture record is stamped source_tier == 4."""
        monkeypatch.setattr(settings, "LEADHUNTER_ENV", "test")
        _, companies, meta = FixtureSource().discover(
            industry="Roofing", location="Dallas Texas", limit=3
        )
        assert companies
        assert all(c.get("source_tier") == 4 for c in companies)
        assert meta.get("source_tier") == 4

    def test_fixture_source_tier_constant(self, monkeypatch):
        """The source advertises its own Tier-4 label."""
        monkeypatch.setattr(settings, "LEADHUNTER_ENV", "test")
        source = FixtureSource()
        assert source.source_tier == int(SourceTier.FIXTURE)


# ---------------------------------------------------------------------------
# Source tier mapping
# ---------------------------------------------------------------------------


class TestSourceTiers:
    def test_tier1_sources(self):
        assert tier_for_source("official_website") == SourceTier.OFFICIAL
        assert tier_for_source("government") == SourceTier.OFFICIAL
        assert tier_for_source("licensing_board") == SourceTier.OFFICIAL
        assert tier_for_source("licensing") == SourceTier.OFFICIAL

    def test_tier2_sources(self):
        assert tier_for_source("trade_association") == SourceTier.TRUSTED
        assert tier_for_source("business_directory") == SourceTier.TRUSTED
        assert tier_for_source("local_chamber") == SourceTier.TRUSTED

    def test_tier3_sources(self):
        assert tier_for_source("search_provider") == SourceTier.SEARCH
        assert tier_for_source("aggregator") == SourceTier.SEARCH
        assert tier_for_source("directory_crawl") == SourceTier.SEARCH

    def test_tier4_sources(self):
        assert tier_for_source("fixture_bridge") == SourceTier.FIXTURE
        assert tier_for_source("fixture") == SourceTier.FIXTURE
        assert tier_for_source("demo") == SourceTier.FIXTURE
        assert tier_for_source("texas_procurement") == SourceTier.FIXTURE

    def test_source_type_fallback(self):
        """An unknown source name can still be tiered by source_type."""
        assert (
            tier_for_source("some_unknown_source", source_type="government")
            == SourceTier.OFFICIAL
        )

    def test_unknown_source_defaults_to_tier3(self):
        """Unclassified sources default to Tier 3, never Tier 1/2/4."""
        assert tier_for_source("mystery_source") == SourceTier.SEARCH

    def test_is_live_tier(self):
        assert is_live_tier(1) is True
        assert is_live_tier(SourceTier.OFFICIAL) is True
        assert is_live_tier(3) is True
        assert is_live_tier(4) is False
        assert is_live_tier(SourceTier.FIXTURE) is False

    def test_tier_label(self):
        assert tier_label(1) == TIER_LABELS[SourceTier.OFFICIAL]
        assert tier_label(SourceTier.FIXTURE) == TIER_LABELS[SourceTier.FIXTURE]


# ---------------------------------------------------------------------------
# Verification data model defaults (accuracy Rules 1 & 8)
# ---------------------------------------------------------------------------


class TestVerificationModels:
    def test_status_values(self):
        assert VerificationStatus.verified.value == "verified"
        assert VerificationStatus.partially_verified.value == "partially_verified"
        assert VerificationStatus.rejected.value == "rejected"
        assert VerificationStatus.unknown.value == "unknown"

    def test_field_evidence_autostamps_fetched_at(self):
        ev = FieldEvidence(
            field="city",
            value="Dallas",
            source="official_website",
            source_url="https://acme-roofing.example",
            confidence=0.9,
            is_reliable=True,
        )
        assert ev.fetched_at  # auto-stamped timestamp

    def test_lead_record_defaults_to_unknown_no_evidence(self):
        """No evidence = unknown, not true (Rule 8); location never filled
        from a query (Rule 1)."""
        rec = LeadRecord(company_name="Acme Roofing LLC", source="fixture_bridge")
        assert rec.verification_status == VerificationStatus.unknown
        assert rec.verification_confidence == 0.0
        assert rec.verification_reasons == []
        assert rec.city is None
        assert rec.state is None
        assert rec.source_tier == int(SourceTier.FIXTURE)
        assert rec.identity_evidence == []
        assert rec.industry_evidence == []
        assert rec.location_evidence == []
