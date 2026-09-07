"""Configurable discovery + research runner (CLI) — thin wrapper.

The actual orchestration (discovery looped to target, then the AI research
pipeline) lives in ``app.leads.pipeline`` so BOTH the CLI and the Leads API
job runner drive one shared pipeline (CLAUDE.md §14 — no duplication).

The user tells the software three things and it runs the whole pipeline:

    WHAT    - the trade to search, e.g. "general contractor", "roofing"
    WHERE   - the location, e.g. "Texas", "Florida", "UK"  (whole world)
    HOW MANY- target number of emails/leads, e.g. 10, 20, 30

Input can come from a JSON config file (reusable query presets) or CLI flags:

    python scripts/run_query.py --config queries/gc-texas.json
    python scripts/run_query.py --trade "general contractor" --location Texas --emails 20
    python scripts/run_query.py --trade roofing --location Florida --emails 30

Config file shape (queries/<name>.json):
    {
      "trade": "general contractor",
      "location": "Texas",
      "target_emails": 20,
      "discover_only": false
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Ensure backend is on path (same as research_lead.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.leads.pipeline import (  # noqa: E402
    ResearchQuery,
    discover_until_target,
    run_research,
)


def query_from_args(args: argparse.Namespace) -> ResearchQuery:
    """Merge a JSON config file with CLI flag overrides (CLI wins)."""
    q = ResearchQuery()
    if args.config:
        data = json.loads(Path(args.config).read_text(encoding="utf-8"))
        q.trade = str(data.get("trade", ""))
        q.location = str(data.get("location", ""))
        q.target_emails = int(data.get("target_emails", 20))
        q.discover_only = bool(data.get("discover_only", False))
    if args.trade:
        q.trade = args.trade
    if args.location:
        q.location = args.location
    if args.emails:
        q.target_emails = args.emails
    if args.discover_only:
        q.discover_only = True
    q.validate()
    return q


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="JSON query config file (queries/<name>.json)")
    parser.add_argument("--trade", help="WHAT to search, e.g. 'general contractor'")
    parser.add_argument("--location", help="WHERE, e.g. 'Texas' / 'Florida' (any region)")
    parser.add_argument("--emails", type=int, help="HOW MANY emails/leads to aim for")
    parser.add_argument("--discover-only", action="store_true", help="Skip AI research phase")
    parser.add_argument("--out", default="output/query_results.json", help="Results JSON path")
    args = parser.parse_args()

    query = query_from_args(args)
    query.validate()
    print("=" * 60)
    print("QUERY")
    print("=" * 60)
    print("  " + query.describe())
    print("=" * 60)

    t0 = time.monotonic()
    print("\nPHASE 1 - DISCOVERY (plan-holder PDFs + search, looped to target)")
    print("-" * 60)
    leads, pass_log = discover_until_target(query)
    print("-" * 60)
    print(f"Discovery complete: {len(leads)} email leads in {time.monotonic() - t0:.0f}s")
    if leads:
        for i, lead in enumerate(leads, 1):
            print(f"  [{i:>2}] {lead['email']:<45} {lead['company'][:35]}")
    print("=" * 60)

    if query.discover_only or not leads:
        print("Stopping after discovery (--discover-only or no leads).")
        return 0

    print("\nPHASE 2 - AI RESEARCH (full pipeline per lead)")
    print("-" * 60)
    results = run_research(leads)

    out = Path(args.out)
    out.parent.mkdir(exist_ok=True)
    payload = {
        "query": query.describe(),
        "trade": query.trade,
        "location": query.location,
        "target_emails": query.target_emails,
        "leads_found": len(leads),
        "discovery_passes": pass_log,
        "results": results,
    }
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("\n" + "=" * 60)
    print(f"COMPLETE: {len(results)} leads -> {out}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
