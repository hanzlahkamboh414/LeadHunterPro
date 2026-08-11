"""Offline tests for Increment8 — the live-coverage run harness.

The harness logic is fully offline: every live stage (discovery,
LeadPipeline) is injected, so the bridge, coverage math, and export are
pinned with canned data. The LIVE run (real network) stays a manual step —
``python run_leads.py``.
"""

from __future__ import annotations

import json

from app.connectors.connector_result import ConnectorResult
from app.engines.lead.lead_samples import (
    sample_low_confidence_lead,
    sample_qualified_lead,
)
from run_leads import build_coverage, run_leads, to_company_discovery

# -- build blocks --------------------------------------------------------


def _connector_result(
    *,
    name: str,
    website: str,
    gate_accepted: bool = True,
) -> ConnectorResult:
    return ConnectorResult(
        company_name=name,
        website=website,
        city="Dallas",
        state="TX",
        source="texas_procurement",
        source_url="https://directory.example/" + name.replace(" ", "-").lower(),
        metadata={
            "gate_accepted": gate_accepted,
            "discovery_reason": "matched roofing contractor",
            "verification": {"accepted": gate_accepted, "verification_status": "verified"},
        },
    )


def _metadata(data_source: str = "live") -> dict:
    return {"data_source": data_source, "source_metadata": {"bridge_mode": False}}


def _discover(companies: list[ConnectorResult], data_source: str = "live"):
    """A stub discovery callable matching the real connector's signature."""

    def _fn(industry: str, location: str, limit: int):
        return companies, _metadata(data_source)

    return _fn


class _StubPipeline:
    """Minimal ``qualify_company`` seam for the harness."""

    def __init__(self, lead_for):
        self.lead_for = lead_for  # callable(company) -> Lead
        self.called: list = []

    def qualify_company(self, company, query=None):
        self.called.append((company, query))
        return self.lead_for(company)


def _run(discover_fn, pipeline, tmp_path):
    return run_leads(
        industry="Roofing",
        location="Dallas Texas",
        limit=5,
        discover_fn=discover_fn,
        pipeline=pipeline,
        out_dir=tmp_path,
    )


class TestRun:

    def test_verified_companies_flow_through_pipeline(self, tmp_path):
        discover_fn = _discover(
            [
                _connector_result(name="A Roofing Co", website="https://a.example"),
                _connector_result(name="B Roofing Co", website="https://b.example"),
            ]
        )
        pipeline = _StubPipeline(lambda company: sample_qualified_lead())

        coverage = _run(discover_fn, pipeline, tmp_path)

        assert len(pipeline.called) == 2
        assert coverage["total_discovered"] == 2
        assert coverage["verified"] == 2
        assert coverage["unverified"] == 0
        assert coverage["qualified"] == 2
        assert coverage["blocked"] == 0
        assert coverage["data_source"] == "live"
        assert coverage["primary_blockers"] == {}
        assert coverage["confidence_gated_at"]["90"] == 0

    def test_unverified_companies_skip_pipeline(self, tmp_path):
        discover_fn = _discover(
            [
                _connector_result(
                    name="Verified Roofing", website="https://v.example", gate_accepted=True
                ),
                _connector_result(
                    name="Bridge Roofing", website="https://b.example", gate_accepted=False
                ),
            ]
        )
        pipeline = _StubPipeline(lambda company: sample_qualified_lead())

        coverage = _run(discover_fn, pipeline, tmp_path)

        assert len(pipeline.called) == 1  # only the verified company is scored
        assert coverage["verified"] == 1
        assert coverage["unverified"] == 1
        assert coverage["qualified"] == 1
        unverified_lead = coverage["leads"][1]
        assert unverified_lead.verified_context is False
        assert unverified_lead.qualification_gate().blocked_by[0].startswith(
            "company not verified"
        )

    def test_fixture_bridge_reported_honestly(self, tmp_path):
        discover_fn = _discover([], data_source="fixture")
        pipeline = _StubPipeline(lambda company: sample_qualified_lead())

        coverage = _run(discover_fn, pipeline, tmp_path)

        assert coverage["data_source"] == "fixture"
        assert coverage["total_discovered"] == 0


class TestCoverageMath:

    def test_primary_blockers_histogram(self):
        leads = [sample_qualified_lead(), sample_low_confidence_lead()]

        coverage = build_coverage(leads, _metadata())

        assert coverage["qualified"] == 1
        assert coverage["blocked"] == 1
        assert coverage["primary_blockers"]["ai_confidence 40 < 90"] == 1

    def test_confidence_gated_at_thresholds(self):
        def _conf(confidence):
            lead = sample_qualified_lead()
            lead.ai.ai_confidence = float(confidence)
            return lead

        leads = [sample_qualified_lead(), _conf(85), _conf(40)]

        coverage = build_coverage(leads, _metadata())

        gated = coverage["confidence_gated_at"]
        assert gated["80"] == 1  # the 85-only lead would clear at 80/85
        assert gated["85"] == 1
        assert gated["90"] == 0  # current threshold blocks it
        assert gated["95"] == 0

    def test_ai_confidence_stats_over_scored_leads_only(self):
        leads = [sample_qualified_lead(), sample_low_confidence_lead()]

        coverage = build_coverage(leads, _metadata())

        conf = coverage["ai_confidence"]
        assert conf is not None
        assert conf["min"] == 40.0
        assert conf["max"] == 91.0
        assert conf["avg"] == round((40.0 + 91.0) / 2, 1)


class TestBridgeAndExport:

    def test_to_company_discovery_preserves_gate_metadata(self):
        result = _connector_result(name="Verified Roofing", website="https://v.example")

        cd = to_company_discovery(result)

        assert cd.metadata["gate_accepted"] is True
        assert cd.website == "https://v.example"
        assert cd.city == "Dallas"
        assert cd.state == "TX"
        assert cd.source == "texas_procurement"
        assert cd.discovery_reason == "matched roofing contractor"

    def test_export_writes_json_and_xlsx(self, tmp_path):
        discover_fn = _discover(
            [_connector_result(name="Verified Roofing", website="https://v.example")]
        )

        coverage = _run(discover_fn, _StubPipeline(lambda c: sample_qualified_lead()), tmp_path)

        export = coverage["export"]
        assert export["json"].exists()
        assert export["xlsx"].exists()
        records = json.loads(export["json"].read_text(encoding="utf-8"))
        assert len(records) == 1
        row = records[0]
        for key in ("company_name", "qualified", "blocked_by", "ai_confidence", "justification"):
            assert key in row
        assert row["qualified"] is True
