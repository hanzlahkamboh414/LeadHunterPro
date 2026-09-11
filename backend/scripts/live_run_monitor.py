"""Live end-to-end run to the dashboard DB, with file telemetry for monitoring.

Verifies (post Galti #1 + #2):
  - research consumers do NOT crash on the DB (busy_timeout + retry guard)
  - email-less plan-holder junk is gated at the source (relevance gate)
  - real plan-holder rows survive into dossiers that reach the dashboard DB

Writes streaming telemetry to ``output/live_run.log`` and a final summary to
``output/live_run_summary.json``. Run from ``backend/``.

Usage:
    python scripts/live_run_monitor.py --trade "General Contractors" --location "Houston TX" --emails 8
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")

from app.core.config import settings  # noqa: E402
from app.lead_research.service import LeadResearchStore  # noqa: E402
from app.leads.pipeline import ResearchQuery, run_full  # noqa: E402

OUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "output"
LOG_PATH = OUT_DIR / "live_run.log"
SUMMARY_PATH = OUT_DIR / "live_run_summary.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade", default="General Contractors")
    parser.add_argument("--location", default="Houston TX")
    parser.add_argument("--emails", type=int, default=8, help="working target")
    parser.add_argument("--discover-only", action="store_true")
    args = parser.parse_args()

    OUT_DIR.mkdir(exist_ok=True)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    file_h = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_h.setFormatter(fmt)
    root.addHandler(file_h)
    console_h = logging.StreamHandler()
    console_h.setFormatter(fmt)
    root.addHandler(console_h)
    root.setLevel(logging.INFO)
    for name in (
        "app.search_providers.manager",
        "app.search_providers.tavily",
        "app.search_providers.searxng",
        "app.discovery.sources.plan_holder_source",
        "app.discovery.pdf_plan_holder_parser",
        "app.discovery.query_expansion",
        "app.leads.pipeline",
        "app.lead_research.service",
    ):
        logging.getLogger(name).setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    print(f"== LIVE RUN: trade={args.trade!r} loc={args.location!r} target={args.emails} ==")
    print(f"   key3 configured: {bool(settings.AI_API_KEY_3)} (query expansion)")
    print(f"   telemetry -> {LOG_PATH}")
    store = LeadResearchStore(db_path=str(OUT_DIR / "lead_research.db"))
    query = ResearchQuery(
        trade=args.trade,
        location=args.location,
        target_emails=args.emails,
        discover_only=args.discover_only,
    )
    t0 = time.time()
    outcome = run_full(query, store=store)
    dt = time.time() - t0

    summary = {
        "query": query.describe(),
        "trade": args.trade,
        "location": args.location,
        "target_emails": args.emails,
        "elapsed_s": round(dt, 1),
        "working_leads": outcome.get("working_leads"),
        "leads_researched": len(outcome.get("results", [])),
        "shortfall": outcome.get("shortfall"),
        "shortfall_reason": outcome.get("shortfall_reason"),
        "working": [
            {"email": e.get("email"), "company": e.get("company")}
            for e in outcome.get("results", [])
            if e.get("working")
        ],
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("=" * 70)
    print(f"== OUTCOME in {dt:.1f}s == (summary -> {SUMMARY_PATH})")
    for k in ("working_leads", "shortfall", "shortfall_reason"):
        print(f"  {k}: {outcome.get(k)}")
    print(f"  researched: {len(outcome.get('results', []))}")
    for e in outcome.get("results", []):
        if e.get("working"):
            print(f"    WORKING: {e.get('email')} ({e.get('company')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())