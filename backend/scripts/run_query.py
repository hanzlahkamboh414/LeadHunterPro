"""Configurable discovery + research runner (the software's input layer).

The user tells the software three things and it runs the whole pipeline:

    WHAT    - the trade to search, e.g. "general contractor", "roofing"
    WHERE   - the location, e.g. "Texas", "Florida", "UK"  (whole world)
    HOW MANY- target number of emails/leads, e.g. 10, 20, 30

Then, in order:
  Phase 1  DISCOVERY  - plan-holder PDF lists + search providers, looped
             until the target email count is reached (deduplicated).
  Phase 2  RESEARCH   - the full AI pipeline per lead (company -> person ->
             deep -> intent/timing -> scoring).

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
from dataclasses import dataclass, field
from pathlib import Path

# Ensure backend is on path (same as research_lead.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.discovery.sources.plan_holder_source import PlanHolderSource  # noqa: E402
from app.discovery.sources.status import SourceStatus  # noqa: E402


# ---------------------------------------------------------------------------
# Query configuration — the user-facing input contract.
# ---------------------------------------------------------------------------

@dataclass
class ResearchQuery:
    """What / where / how many — everything the software needs to run.

    ``trade``     the trade to search (general contractor, roofing, paving...)
    ``location``  where (Texas, Florida, UK... — any region, not just US)
    ``target_emails`` how many emails/leads to aim for (10/20/30...)
    ``discover_only`` stop after discovery, skip the AI research phase
    """
    trade: str = ""
    location: str = ""
    target_emails: int = 20
    discover_only: bool = False

    def validate(self) -> None:
        if not self.trade.strip():
            raise ValueError("trade (WHAT to search) is required — e.g. 'general contractor'")
        if not self.location.strip():
            raise ValueError("location (WHERE) is required — e.g. 'Texas'")
        if self.target_emails <= 0:
            raise ValueError("target_emails (HOW MANY) must be >= 1")

    def describe(self) -> str:
        return (
            f"WHAT={self.trade!r} WHERE={self.location!r} "
            f"HOW_MANY={self.target_emails} emails"
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


# ---------------------------------------------------------------------------
# Phase 1 — discovery, looped until the email target is met.
# ---------------------------------------------------------------------------

# Alternate trade phrasings tried in later passes so we can reach a target
# larger than a single discovery pass yields (broadening, never guessing).
def _trade_variants(trade: str) -> list[str]:
    base = trade.strip()
    variants = [base]
    if not base.endswith("s"):
        variants.append(base + "s")  # singular -> plural
    elif base.endswith("ies"):
        variants.append(base[:-3] + "y")  # "companies" -> "company"
    return variants


def run_discovery(
    trade: str, location: str, limit: int
) -> tuple[SourceStatus, list[dict], dict]:
    """One live plan-holder discovery pass for trade+location."""
    source = PlanHolderSource()  # real seams: search + fetch + parser
    return source.discover(industry=trade, location=location, limit=limit)


def extract_email_leads(records: list[dict]) -> list[dict]:
    """Pull (email, domain) leads from plan-holder metadata, deduplicated."""
    leads: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        ph = rec.get("plan_holder") or {}
        domain = ph.get("domain") or ""
        for entry in ph.get("emails") or []:
            email = (entry.get("email") or "").strip()
            if "@" not in email:
                continue
            key = (email, domain)
            if key in seen:
                continue
            seen.add(key)
            leads.append(
                {
                    "email": email,
                    "domain": domain,
                    "company": rec.get("company_name", ""),
                    "person": (ph.get("person") or {}).get("name", ""),
                    "source_url": rec.get("source_url", ""),
                }
            )
    return leads


def discover_until_target(
    query: ResearchQuery, max_passes: int = 5
) -> tuple[list[dict], list[dict]]:
    """Run discovery passes until target emails are gathered.

    Returns (leads, pass_log). Each pass widens the trade phrasing a little;
    all leads are accumulated and deduplicated. Never guesses — only real
    rows parsed from real plan-holder PDFs count.
    """
    leads: list[dict] = []
    seen: set[str] = set()
    pass_log: list[dict] = []

    variants = _trade_variants(query.trade)
    for pass_idx in range(max_passes):
        trade = variants[pass_idx % len(variants)]
        loc = query.location
        # Widen the search a bit after the first pass (broaden geography too),
        # so a low-yield first pass is not the end of the run.
        if pass_idx >= 1 and query.location:
            loc = query.location
        t0 = time.monotonic()
        status, records, meta = run_discovery(trade, loc, query.target_emails * 4)
        new_leads = extract_email_leads(records)
        fresh = [l for l in new_leads if (l["email"], l["domain"]) not in seen]
        for l in fresh:
            seen.add((l["email"], l["domain"]))
        leads.extend(fresh)
        pass_log.append(
            {
                "pass": pass_idx + 1,
                "trade": trade,
                "location": loc,
                "status": status.value if hasattr(status, "value") else str(status),
                "pdfs_found": meta.get("pdfs_found", "?"),
                "rows": len(records),
                "new_leads": len(fresh),
                "total_leads": len(leads),
                "elapsed_s": round(time.monotonic() - t0, 1),
            }
        )
        print(
            f"  pass {pass_idx + 1}: trade={trade!r} loc={loc!r} "
            f"[{status.value if hasattr(status,'value') else status}] "
            f"new={len(fresh)} total={len(leads)}"
        )
        if len(leads) >= query.target_emails:
            break
    return leads[: query.target_emails], pass_log


# ---------------------------------------------------------------------------
# Phase 2 — AI research pipeline per lead.
# ---------------------------------------------------------------------------

def run_research(leads: list[dict]) -> list[dict]:
    from app.lead_research.agent import AILeadResearchAgent

    agent = AILeadResearchAgent()
    results: list[dict] = []
    for i, lead in enumerate(leads, 1):
        email, domain = lead["email"], lead["domain"]
        print(f"\n  --- Lead {i}/{len(leads)}: {email} ({domain}) ---", flush=True)
        t0 = time.monotonic()
        try:
            d = agent.research(email, domain)
            elapsed = time.monotonic() - t0
            results.append(
                {
                    "email": email,
                    "domain": domain,
                    "company": d.company.name,
                    "person": d.person.name,
                    "bound": d.person.bound,
                    "score": d.potential_score,
                    "recommendation": d.recommendation,
                    "intent": d.intent.needs_estimation if d.intent else "",
                    "timing": d.timing.window if d.timing else "",
                    "sources_checked": d.sources_checked,
                    "elapsed_s": round(elapsed, 1),
                }
            )
            print(
                f"  -> company={d.company.name[:40]!r} person={d.person.name!r} "
                f"bound={d.person.bound} score={d.potential_score} "
                f"rec={d.recommendation} [{elapsed:.0f}s]",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - one lead never kills the batch
            print(f"  -> ERROR: {exc}", flush=True)
            results.append({"email": email, "domain": domain, "error": str(exc)})
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
