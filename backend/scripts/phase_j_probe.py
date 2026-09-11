"""Phase J one-shot probe: run ONE real job and print the search query / PDF
flow so the AI-expanded query surface can be monitored end to end.

Usage (from backend/):
    python scripts/phase_j_probe.py
"""
from __future__ import annotations

import logging
import pathlib
import sys
import time

# Make 'app' importable regardless of how the script is launched — python adds
# scripts/ to sys.path, not the backend root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
# Surface the providers/search + plan-holder parse lines specifically.
for name in (
    "app.search_providers.manager",
    "app.search_providers.tavily",
    "app.search_providers.searxng",
    "app.discovery.sources.plan_holder_source",
    "app.discovery.pdf_plan_holder_parser",
    "app.discovery.query_expansion",
    "app.leads.pipeline",
):
    logging.getLogger(name).setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

from app.lead_research.service import LeadResearchStore  # noqa: E402
from app.leads.pipeline import ResearchQuery, run_full  # noqa: E402

TRADE = "General Contractors"
LOCATION = "Houston TX"
TARGET = 6

if __name__ == "__main__":
    print(f"== Phase J probe: trade={TRADE!r} location={LOCATION!r} target={TARGET} ==")
    t0 = time.time()
    store = LeadResearchStore(db_path="output/lead_research.db")
    query = ResearchQuery(trade=TRADE, location=LOCATION, target_emails=TARGET)
    outcome = run_full(query, store=store)
    dt = time.time() - t0
    print("=" * 70)
    print(f"== OUTCOME in {dt:.1f}s ==")
    for k in ("working_leads", "leads_found", "shortfall", "shortfall_reason"):
        print(f"  {k}: {outcome.get(k)}")
    working = [e for e in outcome.get("results", []) if e.get("working")]
    print(f"  working emails ({len(working)}):")
    for e in working:
        print(f"    - {e.get('email')} ({e.get('company')})")