"""Increment8 — live coverage run: discovery -> LeadPipeline -> V1 leads -> export.

Drives the EXISTING discovery pipeline and :class:`LeadPipeline` (rule #14 —
no new engine) over a real Texas query and reports honest coverage: how many
companies were discovered, how many cleared the AcceptanceGate, and how many
qualified through the V1 gate — with every blocker, so a lead that did not
qualify reports exactly why (CLAUDE.md §1, §10). Nothing is fabricated; when
the only source is the fixture bridge, that is reported, never hidden.

Every live stage is injectable (``discover_fn`` / ``pipeline``) so the harness
logic is fully offline-testable; the LIVE run is a manual step:

    python run_leads.py "Roofing" "Dallas Texas" 20
    python run_leads.py "Roofing" "Dallas Texas" 20 --output output/leads

Non-verified companies (incl. fixture-bridge records) are reported honestly
with ``company not verified`` as their blocker and are NOT sent through the
expensive live stages — the V1 verdict cannot change without the gate's
first rule, so spending live network on them would be wasted work.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.connectors.connector_result import ConnectorResult
from app.engines.discovery.company.company_models import CompanyDiscoveryResult
from app.engines.lead.lead_models import Lead
from app.engines.lead.lead_pipeline import LeadPipeline, lead_to_export_row

#: Thresholds probed for the confidence-only-gated analysis (the calibration
#: signal for QUALIFIED_THRESHOLD). The current V1 gate keeps 90.
THRESHOLD_PROBES = (80, 85, 90, 95)

#: Ordered export columns — mirrors ``lead_to_export_row`` keys so the reused
#: xlsx writer emits the same contract demo.py does.
LEAD_EXPORT_FIELDS = (
    "company_name",
    "website",
    "city",
    "state",
    "industry",
    "source",
    "source_url",
    "gate_accepted",
    "decision_maker",
    "decision_maker_role",
    "person_bound_email",
    "intent_evidence",
    "ai_confidence",
    "justification",
    "deterministic_score",
    "ai_score",
    "ai_used",
    "qualified",
    "blocked_by",
    "created_at",
)

DiscoveryFn = Callable[[str, str, int], tuple[list[Any], dict[str, Any]]]


# ---------------------------------------------------------------------------
# Discovery + bridge
# ---------------------------------------------------------------------------


def discover(industry: str, location: str, limit: int) -> tuple[list[Any], dict[str, Any]]:
    """The real discovery pipeline (TexasProcurementConnector)."""
    from app.connectors.texas_procurement import TexasProcurementConnector

    return TexasProcurementConnector().search(industry, location, limit)


def to_company_discovery(result: ConnectorResult) -> CompanyDiscoveryResult:
    """Bridge a ConnectorResult onto the LeadPipeline's company contract.

    The connector's metadata already carries the authoritative gate verdict
    (``gate_accepted`` + ``verification``), so it is carried verbatim — the
    V1 lead gate reads exactly that key. Location/source map 1:1; nothing is
    invented here.
    """
    meta = dict(result.metadata or {})
    return CompanyDiscoveryResult(
        company_name=result.company_name,
        website=result.website,
        city=result.city,
        state=result.state,
        country=result.country,
        source=result.source,
        confidence=result.confidence,
        source_url=result.source_url,
        discovery_reason=meta.get("discovery_reason", ""),
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# Coverage math
# ---------------------------------------------------------------------------


def _confidence_gated_count(leads: list[Lead], threshold: int) -> int:
    """Leads that would clear the gate if the confidence rule were *threshold*.

    Counts only leads that are NOT already qualified and whose ONLY blocker is
    the confidence rule (every other hard rule met) — the calibration signal
    for QUALIFIED_THRESHOLD.
    """
    count = 0
    for lead in leads:
        blockers = lead.qualification_gate().blocked_by
        if not blockers:
            continue  # already qualifies at any threshold
        non_confidence = [b for b in blockers if not b.startswith("ai_confidence")]
        if not non_confidence and lead.ai.ai_confidence >= threshold:
            count += 1
    return count


def build_coverage(
    leads: list[Lead],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Honest coverage summary: verdicts, blockers, and threshold signal."""
    qualified = [l for l in leads if l.qualifies]
    verified = [l for l in leads if l.verified_context]
    primary = Counter()
    for lead in leads:
        if lead.qualifies:
            continue
        blockers = lead.qualification_gate().blocked_by
        primary[blockers[0] if blockers else "(no blocker)"] += 1
    scored = [l.ai.ai_confidence for l in verified]
    return {
        "total_discovered": len(leads),
        "verified": len(verified),
        "unverified": len(leads) - len(verified),
        "qualified": len(qualified),
        "blocked": len(leads) - len(qualified),
        "ai_used": sum(1 for l in leads if l.ai.ai_used),
        "ai_error": sum(1 for l in leads if l.ai.error),
        "ai_confidence": (
            {
                "min": min(scored),
                "avg": round(sum(scored) / len(scored), 1),
                "max": max(scored),
            }
            if scored
            else None
        ),
        "primary_blockers": dict(primary.most_common()),
        "confidence_gated_at": {
            str(t): _confidence_gated_count(leads, t) for t in THRESHOLD_PROBES
        },
        "qualified_rows": [lead_to_export_row(l) for l in qualified],
    }


# ---------------------------------------------------------------------------
# Export (reuses the demo.py exporter contract — rule #14)
# ---------------------------------------------------------------------------


def _cell_text(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    return "" if value is None else str(value)


def _write_lead_xlsx(path: Path, records: list[dict[str, Any]]) -> None:
    headers = list(LEAD_EXPORT_FIELDS)
    rows = [headers] + [
        [_cell_text(rec.get(h, "")) for h in headers] for rec in records
    ]
    try:
        from openpyxl import Workbook
    except ImportError:
        from demo import _write_xlsx_zip

        _write_xlsx_zip(path, rows)
    else:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Leads"
        for row in rows:
            sheet.append(row)
        workbook.save(path)


def export_leads(leads: list[Lead], *, out_dir: Path) -> dict[str, Path]:
    """Write leads.json + leads.xlsx (V1 export row shape)."""
    records = [lead_to_export_row(l) for l in leads]
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "leads.json"
    json_path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    xlsx_path = out_dir / "leads.xlsx"
    _write_lead_xlsx(xlsx_path, records)
    return {"json": json_path, "xlsx": xlsx_path}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_leads(
    *,
    industry: str,
    location: str,
    limit: int,
    discover_fn: DiscoveryFn | None = None,
    pipeline: Any | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Full coverage run; returns the coverage dict plus leads/metadata/export."""
    discover_fn = discover_fn if discover_fn is not None else discover
    if pipeline is None:
        pipeline = LeadPipeline()
    query: dict[str, Any] = {"industry": industry, "location": location}

    results, metadata = discover_fn(industry, location, limit)
    companies = [to_company_discovery(r) for r in results]

    leads: list[Lead] = []
    for company in companies:
        if not company.metadata.get("gate_accepted"):
            leads.append(Lead(company=company))
        else:
            leads.append(pipeline.qualify_company(company, query))

    coverage = build_coverage(leads, metadata)
    coverage["query"] = query
    coverage["data_source"] = metadata.get("data_source", "empty")
    orch = metadata.get("source_metadata") or {}
    coverage["bridge_mode"] = bool(orch.get("bridge_mode", False))
    coverage["fallback_reason"] = orch.get("fallback_reason", "")
    coverage["export"] = (
        export_leads(leads, out_dir=out_dir) if out_dir is not None else {}
    )
    coverage["leads"] = leads
    coverage["metadata"] = metadata
    return coverage


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def probe_discovery(industry: str, location: str, limit: int) -> int:
    """Readiness probe — discovery only, no pipeline (for live runs).

    Prints what the real discovery pipeline returns for a query so a full
    run's data_source (live vs fixture bridge) is known before spending the
    time on the live LeadPipeline stages. Reuses the same ``discover()`` the
    full run drives — nothing separate to maintain.
    """
    results, metadata = discover(industry, location, limit)
    data_source = metadata.get("data_source", "empty")
    orch = metadata.get("source_metadata") or {}
    print(f"data_source={data_source}  bridge_mode={orch.get('bridge_mode', False)}")
    if orch.get("fallback_reason"):
        print(f"fallback_reason={orch['fallback_reason']}")
    print(f"total_in_dataset={metadata.get('total_in_dataset')}")
    print(f"total_matched={metadata.get('total_matched')}")
    print(f"returned={len(results)}")
    print("\nPer-source status (orchestrator):")
    source_stats = orch.get("source_stats") or {}
    for name, stats in source_stats.items():
        status = (stats.get("status") or "?").value if hasattr(
            stats.get("status"), "value"
        ) else stats.get("status", "?")
        print(f"  - {name:<24} {status:<12} results={stats.get('results', 0)}")
    for result in results:
        meta = result.metadata or {}
        print(
            f"  - {result.company_name} | {result.website} | "
            f"gate={meta.get('gate_accepted')} | prov={meta.get('data_provenance')}"
        )
    return 0


def _print_report(coverage: dict[str, Any]) -> None:
    query = coverage["query"]
    print(f"\nCoverage: {query['industry']} / {query['location']}")
    print(
        f"Data source:        {coverage['data_source']}"
        + ("  (bridge_mode)" if coverage["bridge_mode"] else "")
    )
    if coverage.get("fallback_reason"):
        print(f"Fallback reason:    {coverage['fallback_reason']}")
    print(f"Discovered:         {coverage['total_discovered']}")
    print(
        f"Gate-verified:      {coverage['verified']}  "
        f"(unverified: {coverage['unverified']})"
    )
    print(f"Qualified (V1):     {coverage['qualified']}")
    print(f"Blocked:            {coverage['blocked']}")
    print(f"AI used / errors:   {coverage['ai_used']} / {coverage['ai_error']}")
    conf = coverage["ai_confidence"]
    if conf:
        print(f"ai_confidence:      min={conf['min']} avg={conf['avg']} max={conf['max']}")
    print("\nPrimary blockers:")
    if not coverage["primary_blockers"]:
        print("  (none)")
    for blocker, count in coverage["primary_blockers"].items():
        print(f"  - {count:>3}  {blocker}")
    print("\nConfidence-gated only (would qualify if threshold were):")
    for threshold, count in coverage["confidence_gated_at"].items():
        marker = " <-- current" if threshold == "90" else ""
        print(f"  >= {threshold}: {count}{marker}")
    if coverage.get("export"):
        print("\nExport:")
        for label, path in coverage["export"].items():
            print(f"  ✓ {label}: {path}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_leads.py",
        description="LeadHunter Pro Inc8 — live coverage run to qualified V1 leads.",
    )
    parser.add_argument("industry", nargs="?", default="Roofing", help="Industry keyword.")
    parser.add_argument(
        "location",
        nargs="?",
        default="Dallas Texas",
        help="Geographic location, e.g. 'Dallas Texas'.",
    )
    parser.add_argument("limit", nargs="?", type=int, default=20, help="Max results.")
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory (default: backend/output/leads).",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Discovery-only readiness probe (no pipeline, no export).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        from demo import _portable_stdout
    except ImportError:  # console is a nice-to-have
        pass
    else:
        _portable_stdout()

    out_dir = (
        Path(args.output)
        if args.output
        else Path(__file__).resolve().parent / "output" / "leads"
    )
    if args.probe:
        print("=" * 60)
        print("Inc8 — LIVE DISCOVERY PROBE (no pipeline)")
        print("=" * 60)
        return probe_discovery(args.industry, args.location, args.limit)

    print("=" * 60)
    print("Inc8 — LIVE COVERAGE RUN")
    print("=" * 60)
    print(f"Query: {args.industry} / {args.location} | limit={args.limit}")

    start = time.monotonic()
    try:
        coverage = run_leads(
            industry=args.industry,
            location=args.location,
            limit=args.limit,
            out_dir=out_dir,
        )
    except Exception as exc:  # noqa: BLE001 — report and stop, never fabricate
        print("\nFAILED AT: Coverage run")
        print("Reason:", exc)
        return 1
    elapsed = time.monotonic() - start
    print(f"\nElapsed: {elapsed:.1f}s")
    _print_report(coverage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
