"""LeadHunter Pro — Prototype V1 Product-Validation Demonstration.

A thin orchestration runner over the EXISTING discovery backend. It creates
no engines, adds no discovery systems, and refactors nothing — every stage
calls existing production code:

    Planning Sources     -> app.engines.source_intelligence.source_planner.SourcePlanner
    Discovery pipeline   -> app.connectors.texas_procurement.TexasProcurementConnector.search
                           (SourceOrchestrator -> DirectoryCrawlSource ->
                            SearchProviderSource -> FixtureSource -> filter -> rank)
    Website enrichment   -> app.engines.website_engine.WebsiteEngine     (--enrich only)
    AI qualification     -> app.engines.ai_engine.AIEngine.score_company
    JSON export          -> stdlib json
    Excel export         -> openpyxl (preferred) or a dependency-free .xlsx writer

Every number printed is the ACTUAL output of the pipeline run — nothing is
fabricated. When no live source can reach its target, the pipeline honestly
reports data_source="fixture" (CLAUDE.md §1, §5): the bridge is printed, never
hidden. If a stage raises, the run stops with FAILED AT / Reason /
Root Cause / Suggested Fix.

Usage (run from backend/):
    python demo.py
    python demo.py "Roofing" "Dallas Texas" 50
    python demo.py --enrich          # also visit each company website before scoring
    python demo.py "Plumbing" "Houston TX" 20 --output results
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

#: A lead is "qualified" when AIEngine returns a score at/above this value.
#: The heuristic scoring engine gives 20 for title (company name) + 10 for
#: description (industry focus) — the fields discovery already produces.
#: Fully-enriched sites (emails/phones/linkedin/contact/about) reach higher.
QUALIFIED_THRESHOLD = 30

#: Output record fields — all produced by the backend, nothing invented.
EXPORT_FIELDS = [
    "company_name",
    "website",
    "city",
    "state",
    "industry",
    "source",
    "discovery_method",
    "lead_score",
    "deterministic_score",
    "ai_score",
    "qualified",
    "qualification",
    "ai_used",
    "confidence",
    "source_url",
]


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------


def _portable_stdout() -> None:
    """Make ✓ and other unicode safe on any Windows console / redirect."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _banner(title: str) -> None:
    print(f"\n-- {title} " + "-" * max(1, 58 - len(title)))


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s"


def _status_str(status: Any) -> str:
    value = getattr(status, "value", status)
    return str(value).upper()


def _fail(stage: str, exc: BaseException) -> int:
    """Stop immediately with a root-cause diagnostic (never silently continue)."""
    print("\nFAILED AT:", stage)
    print("Reason:", exc)
    print("Root Cause:", _root_cause(exc))
    print("Suggested Fix:", _suggested_fix(stage, exc))
    return 1


def _root_cause(exc: BaseException) -> str:
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return (
            "A required module is missing in the current Python environment — "
            "either dependencies are not installed or the demo is not run from "
            "backend/ where app.* is importable."
        )
    if isinstance(exc, PermissionError):
        return "The output directory is not writable."
    if isinstance(exc, OSError):
        return "An OS error occurred while writing output files."
    # Default: an unexpected error in the pipeline stage.
    return "An unexpected error occurred in the pipeline stage."


def _suggested_fix(stage: str, exc: BaseException) -> str:
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return (
            "cd backend && python -m pip install -r requirements/requirements.txt; "
            "then run: python demo.py"
        )
    if isinstance(exc, (PermissionError, OSError)):
        return "Make the output directory writable, or pass --output <other-dir>."
    if stage in ("Planning Sources", "Discovery"):
        return (
            "Check that the discovery sources can run (network for live crawl). "
            "If only the fixture bridge returns data, that is reported honestly "
            "in the summary — the pipeline did not fail."
        )
    return "Inspect the traceback above and correct the failing component."


# ---------------------------------------------------------------------------
# Excel export — openpyxl preferred, dependency-free .xlsx fallback
# ---------------------------------------------------------------------------


def _col_letter(idx: int) -> str:
    letters = ""
    while idx >= 0:
        letters = chr(ord("A") + (idx % 26)) + letters
        idx = idx // 26 - 1
    return letters


def _xml_escape(value: Any) -> str:
    s = str(value)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _write_xlsx_zip(path: Path, rows: list[list[Any]]) -> None:
    """Write a minimal, valid .xlsx using only the stdlib.

    Used only when openpyxl is not installed, so the export stage always
    produces companies.xlsx. Excel / LibreOffice / Google Sheets all open it.
    """
    sheet_rows: list[str] = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{_col_letter(c)}{r}"
            cells.append(
                f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                f"{_xml_escape(value)}</t></is></c>"
            )
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')

    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Companies" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)


def _write_xlsx(path: Path, records: list[dict[str, Any]]) -> None:
    headers = EXPORT_FIELDS
    rows: list[list[Any]] = [headers]
    rows += [[rec.get(h, "") for h in headers] for rec in records]
    try:
        from openpyxl import Workbook
    except ImportError:
        _write_xlsx_zip(path, rows)
    else:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Companies"
        for row in rows:
            sheet.append(row)
        workbook.save(path)


# ---------------------------------------------------------------------------
# Pipeline stages (each calls existing production code)
# ---------------------------------------------------------------------------


def stage_planning(industry: str, location: str) -> tuple[Any, list[Any]]:
    """SourcePlanner.plan() — the planning stage of the real pipeline."""
    from app.connectors.texas_procurement import _parse_location
    from app.engines.source_intelligence.source_models import SourcePlannerRequest
    from app.engines.source_intelligence.source_planner import SourcePlanner

    city, state = _parse_location(location)
    planned = SourcePlanner().plan(
        SourcePlannerRequest(
            industry=industry,
            country="USA",
            state=state,
            city=city,
        )
    )
    crawlable = [s for s in planned.sources if s.supports_company_discovery and s.url]
    return planned, crawlable


def stage_discovery(
    industry: str, location: str, limit: int
) -> tuple[list[Any], dict[str, Any], float]:
    """TexasProcurementConnector.search() — the full discovery pipeline.

    Runs SourceOrchestrator (DirectoryCrawlSource -> SearchProviderSource ->
    FixtureSource), then applies the connector's step-3 filters, URL
    validation and ranking. Returns (ConnectorResults, metadata, elapsed_s).
    """
    from app.connectors.texas_procurement import TexasProcurementConnector

    print("  (live crawl attempted; may take a moment — directory crawl, search, bridge)")
    start = time.monotonic()
    results, metadata = TexasProcurementConnector().search(industry, location, limit)
    elapsed = time.monotonic() - start
    return results, metadata, elapsed


def stage_scoring(
    results: list[Any],
    *,
    enrich: bool,
    enrich_limit: int,
    industry: str,
    location: str,
) -> tuple[list[tuple[Any, dict[str, Any]]], int]:
    """AIEngine.qualify_lead() per company — deterministic + Combo-LeadHunter AI.

    With ``enrich``, each company's own website is fetched/parsed first via
    WebsiteEngine so real site signals (emails, phones, social, contact/about
    pages) feed qualification — the §10 target flow. Enrichment failures keep
    the base evidence; they never fail the run. AI failure falls back to the
    deterministic score inside the engine; it never fails the run either.
    """
    from app.email.email_cleaner import clean_emails
    from app.engines.ai_engine import AIEngine

    engine = AIEngine()
    fetcher = None
    if enrich:
        from app.engines.website_engine import WebsiteEngine

        fetcher = WebsiteEngine()

    query_context: dict[str, Any] = {"industry": industry, "location": location}
    scored: list[tuple[Any, dict[str, Any]]] = []
    enriched = 0
    for result in results:
        data: dict[str, Any] = {
            "title": result.company_name,
            "company_name": result.company_name,
            "website": result.website,
            "city": result.city,
            "state": result.state,
            "trade_category": result.metadata.get("trade_category", ""),
            "industry_focus": result.metadata.get("industry_focus", ""),
            "description": (
                result.metadata.get("industry_focus")
                or result.metadata.get("trade_category")
                or result.metadata.get("discovery_reason", "")
            ),
            "source": result.source,
            "source_url": result.source_url,
        }
        if fetcher is not None and result.website and enriched < enrich_limit:
            try:
                html = fetcher.fetch(result.website)
                parsed = fetcher.parse(html, result.website)
                if parsed.get("title"):
                    data["title"] = parsed["title"]
                if parsed.get("description"):
                    data["description"] = parsed["description"]
                data["emails"] = clean_emails(parsed.get("emails") or [])
                data["phones"] = parsed.get("phones") or []
                data["linkedin"] = parsed.get("linkedin") or []
                data["contact_page"] = parsed.get("contact_page") or ""
                data["about_page"] = parsed.get("about_page") or ""
                enriched += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    - enrich skipped ({result.website}): {type(exc).__name__}")
        scored.append((result, engine.qualify_lead(data, query_context)))
    return scored, enriched


def stage_export(
    scored: list[tuple[Any, int]],
    *,
    industry: str,
    out_dir: Path,
) -> tuple[Path, Path]:
    """Write output/companies.json and output/companies.xlsx."""
    records: list[dict[str, Any]] = []
    for result, q in scored:
        records.append(
            {
                "company_name": result.company_name,
                "website": result.website,
                "city": result.city,
                "state": result.state,
                "industry": (
                    result.metadata.get("industry_focus")
                    or result.metadata.get("trade_category")
                    or industry
                ),
                "source": result.source,
                "discovery_method": (
                    result.metadata.get("data_provenance") or result.source
                ),
                "lead_score": q["score"],
                "deterministic_score": q["deterministic_score"],
                "ai_score": q["ai_score"] if q["ai_score"] is not None else "",
                "qualified": bool(q["score"] >= QUALIFIED_THRESHOLD),
                "qualification": q.get("qualification", "") or "",
                "ai_used": bool(q["ai_used"]),
                "confidence": round(float(result.confidence), 3),
                "source_url": result.source_url,
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "companies.json"
    json_path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    xlsx_path = out_dir / "companies.xlsx"
    _write_xlsx(xlsx_path, records)
    return json_path, xlsx_path


def _print_lead_qualification(
    result: Any,
    q: dict[str, Any],
    *,
    threshold: int,
) -> None:
    """Print one lead's qualification verdict on compact lines."""
    ai_score = q["ai_score"] if q["ai_score"] is not None else "n/a"
    flag = "QUALIFIED" if q["score"] >= threshold else "          "
    error = f"  error={q['error']}" if q["error"] else ""
    print(
        f"  [{flag}] {result.company_name:<40} "
        f"det={q['deterministic_score']:>3} ai={ai_score!s:>4} "
        f"final={q['score']:>3} ai_used={int(q['ai_used'])}{error}"
    )
    if q.get("qualification"):
        print(f"       qualification: {q['qualification']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="demo.py",
        description="LeadHunter Pro Prototype V1 — run the existing discovery pipeline.",
    )
    parser.add_argument("industry", nargs="?", default="Roofing", help="Industry keyword.")
    parser.add_argument(
        "location",
        nargs="?",
        default="Dallas Texas",
        help="Geographic location, e.g. 'Dallas Texas'.",
    )
    parser.add_argument("limit", nargs="?", type=int, default=50, help="Max results to return.")
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Visit each company website (WebsiteEngine) before AI scoring.",
    )
    parser.add_argument(
        "--enrich-limit", type=int, default=10, help="Max websites to visit when --enrich."
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory (default: backend/output).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _portable_stdout()
    args = _parse_args(argv)
    industry, location, limit = args.industry, args.location, args.limit
    out_dir = (
        Path(args.output)
        if args.output
        else Path(__file__).resolve().parent / "output"
    )

    print("=" * 50)
    print("LeadHunter Pro Prototype V1")
    print("=" * 50)
    print("\nQuery:")
    print(f"{industry} Companies {location}")
    print(f"Limit: {limit} | Website enrichment: {'ON' if args.enrich else 'OFF'}")
    total_start = time.monotonic()

    # Stage 1 — Planning Sources
    _banner("Stage 1 — Planning Sources")
    try:
        planned, crawlable = stage_planning(industry, location)
    except Exception as exc:  # noqa: BLE001
        return _fail("Planning Sources", exc)
    print(f"✓ Sources Selected: {planned.total_sources}")
    print(f"✓ Crawlable Seeds:  {len(crawlable)}")
    for seed in crawlable[:8]:
        print(f"    - {seed.name}  ({seed.url})")

    # Stage 2 — Discovery (Source Orchestrator → website discovery → crawl → extraction)
    _banner("Stage 2 — Source Orchestrator / Discovery")
    try:
        results, metadata, discovery_elapsed = stage_discovery(industry, location, limit)
    except Exception as exc:  # noqa: BLE001
        return _fail("Discovery", exc)
    orch_meta = metadata.get("source_metadata", {})
    source_stats: dict[str, dict[str, Any]] = orch_meta.get("source_stats", {})
    data_source = orch_meta.get("data_source", metadata.get("data_source", "empty"))
    print(
        f"✓ Sources Executed: {orch_meta.get('sources_executed', len(source_stats))}"
    )
    for name, stats in source_stats.items():
        print(
            f"    - {name:<18} {_status_str(stats.get('status', '')):<12} "
            f"{stats.get('results', 0)} companies"
        )
    print(f"✓ Data Source: {data_source}  |  bridge_mode: {orch_meta.get('bridge_mode', False)}")
    if orch_meta.get("fallback_reason"):
        print(f"  Fallback reason: {orch_meta['fallback_reason']}")

    # Stage 3 — Website Discovery
    _banner("Stage 3 — Website Discovery")
    websites_found = len({r.website for r in results if r.website})
    print(f"✓ Websites Found: {websites_found}")
    if not websites_found:
        print("  (no company websites in the discovery results)")

    # Stage 4 — Directory Crawl
    _banner("Stage 4 — Directory Crawl")
    crawl_stats = source_stats.get("directory_crawl", {})
    crawl_md: dict[str, Any] = crawl_stats.get("metadata", {})
    pages_fetched = crawl_md.get("pages_fetched", 0)
    pages_attempted = crawl_md.get("pages_attempted", 0)
    seeds_planned = crawl_md.get("seeds_planned", 0)
    fetch_failures = crawl_md.get("fetch_failures", 0)
    print(f"✓ Pages Crawled: {pages_fetched}  (attempted: {pages_attempted})")
    print(f"✓ Seeds Planned: {seeds_planned}")
    print(f"✓ Failed Crawls: {fetch_failures}")

    # Stage 5 — Company Extraction
    _banner("Stage 5 — Company Extraction")
    accepted = crawl_md.get("companies_accepted", 0)
    rejected = crawl_md.get("companies_rejected", 0)
    total_raw = orch_meta.get("total_raw", len(results))
    print(f"✓ Companies Extracted: {total_raw}  (accepted: {accepted}, rejected: {rejected})")

    # Stage 6 — Duplicate Removal
    _banner("Stage 6 — Duplicate Removal")
    total_deduped = orch_meta.get("total_deduped", len(results))
    duplicates_removed = max(0, total_raw - total_deduped)
    print(f"✓ Removed: {duplicates_removed}")
    print(f"✓ Unique Companies: {total_deduped}")

    # Stage 7 — AI Qualification & Lead Scoring
    _banner("Stage 7 — AI Qualification & Lead Scoring")
    try:
        scored, enriched = stage_scoring(
            results,
            enrich=args.enrich,
            enrich_limit=args.enrich_limit,
            industry=industry,
            location=location,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail("AI Qualification", exc)
    scores = [q["score"] for _, q in scored]
    qualified = sum(1 for s in scores if s >= QUALIFIED_THRESHOLD)
    avg_score = sum(scores) / len(scores) if scores else 0.0
    ai_used_count = sum(1 for _, q in scored if q["ai_used"])
    ai_error_count = sum(1 for _, q in scored if q["error"])
    print(f"✓ Scores Computed: {len(scores)}  (min={min(scores) if scores else 0}, "
          f"avg={avg_score:.1f}, max={max(scores) if scores else 0})")
    print(f"✓ Qualified Leads (final score >= {QUALIFIED_THRESHOLD}): {qualified}")
    print(f"✓ AI Qualification Used: {ai_used_count}/{len(scores)}  "
          f"(fallbacks/errors: {ai_error_count})")
    if args.enrich:
        print(f"✓ Websites Enriched: {enriched}")
    for result, q in scored:
        _print_lead_qualification(result, q, threshold=QUALIFIED_THRESHOLD)

    # Stage 8 — Export
    _banner("Stage 8 — Export")
    try:
        json_path, xlsx_path = stage_export(
            scored,
            industry=industry,
            out_dir=out_dir,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail("Export", exc)
    print(f"✓ {json_path.name}  -> {json_path}")
    print(f"✓ {xlsx_path.name}  -> {xlsx_path}")

    # Final report
    total_elapsed = time.monotonic() - total_start
    crawl_time_per_page = (
        discovery_elapsed / pages_fetched if pages_fetched else None
    )
    accuracy = (
        round(100 * accepted / (accepted + rejected), 1)
        if (accepted + rejected) > 0
        else None
    )

    print("\n" + "=" * 50)
    print("Discovery Summary")
    print("=" * 50)
    print(f"\nData Source:            {data_source}")
    print(f"Total Time:             {_format_duration(total_elapsed)}")
    print("\nSources Used:")
    if not source_stats:
        print("  (none executed)")
    for name, stats in source_stats.items():
        print(f"  - {name:<18} {_status_str(stats.get('status', '')):<12} "
              f"{stats.get('results', 0)} companies")
    print()
    print(f"Websites Found:         {websites_found}")
    print(f"Pages Crawled:          {pages_fetched}")
    print(f"Companies Extracted:    {total_raw}")
    print(f"Duplicates Removed:     {duplicates_removed}")
    print(f"Unique Companies:       {total_deduped}")
    print(f"Qualified Leads:        {qualified}  (score >= {QUALIFIED_THRESHOLD})")
    print(f"Failed Crawls:          {fetch_failures}")
    print(
        "Average Crawl Time:     "
        + (f"{crawl_time_per_page:.1f}s/page" if crawl_time_per_page is not None else "n/a")
    )
    print(f"Extraction Accuracy:    {accuracy if accuracy is not None else 'n/a'}%")
    print("\nExport:")
    print(f"  ✓ {json_path.name}")
    print(f"  ✓ {xlsx_path.name}")

    # Validation block
    print("\n" + "=" * 50)
    print("Validation")
    print("=" * 50)
    print(f"Discovery Accuracy:    {accuracy if accuracy is not None else 'n/a'}% "
          f"(accepted / (accepted+rejected))")
    print(f"Sources Used:          {', '.join(source_stats) or 'none'}")
    print(f"Companies Extracted:   {total_raw}")
    print(f"Duplicates Removed:    {duplicates_removed}")
    print(f"Final Companies:       {len(scored)}")
    print(f"Qualified Leads:       {qualified}")
    print(f"Failed Crawls:         {fetch_failures}")
    print(
        "Average Crawl Time:    "
        + (f"{crawl_time_per_page:.1f}s/page" if crawl_time_per_page is not None else "n/a")
    )
    print()

    if not scored:
        print("⚠ NO COMPANIES DISCOVERED — see 'Data Source' and 'Sources Used' above.")
        print("  This is reported honestly; the pipeline did not raise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
