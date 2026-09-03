"""Complete run: plan-holder PDF discovery -> AI research pipeline.

Chains two EXISTING pieces (rule #14 — no new engine):

  Phase 1  PlanHolderSource.discover() live
             dorks -> search registry (Tavily) -> .pdf filter -> fetch ->
             PdfPlanHolderParser -> rows with emails/persons/phones.
  Phase 2  research_lead.AILeadResearchAgent.research(email, domain)
             full pipeline: company -> person -> deep -> intent -> scoring.

Phase 1 is the "PDF search first" the lead pipeline depends on: it surfaces
plan-holder lists and extracts a company per row, with the row's real
registered-domain email as the derived website. Emails that are present
in the plan-holder metadata are the leads we hand to Phase 2 (a plan-holder
list only binds a decision maker when the row carries an email).

Usage, from backend/:
    python scripts/run_plan_holder_research.py --industry construction --location Texas --limit 60
    python scripts/run_plan_holder_research.py --discover-only --limit 60   # Phase 1 only
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

from dotenv import load_dotenv

# Load .env so Tavily / AI keys are available (same as research_lead.py).
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.discovery.sources.plan_holder_source import PlanHolderSource  # noqa: E402
from app.discovery.sources.status import SourceStatus  # noqa: E402


def run_discovery(industry: str, location: str, limit: int) -> tuple[list[dict], dict]:
    """Phase 1 — live plan-holder discovery. Returns (records, leads)."""
    source = PlanHolderSource()  # default seams: real search + fetch + parser
    status, records, meta = source.discover(
        industry=industry, location=location, limit=limit
    )
    return status, records, meta


def extract_leads(records: list[dict]) -> list[dict]:
    """Pull (email, domain) leads from plan-holder metadata.

    A lead needs an email (for triage + person binding). The company domain is
    the row's registered domain (in ``plan_holder.domain``) or, failing that,
    derived from the email itself.
    """
    leads: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        ph = rec.get("plan_holder") or {}
        emails = ph.get("emails") or []
        if not emails:
            continue
        domain = ph.get("domain") or ""
        for entry in emails:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--industry", default="construction", help="Industry keyword for dorks")
    parser.add_argument("--location", default="Texas", help="Location keyword for dorks")
    parser.add_argument("--limit", type=int, default=60, help="Max leads to surface")
    parser.add_argument("--discover-only", action="store_true", help="Phase 1 only, no AI research")
    args = parser.parse_args()

    t0 = time.monotonic()
    status, records, meta = run_discovery(args.industry, args.location, args.limit)
    leads = extract_leads(records)

    print("=" * 60)
    print("PHASE 1 - PLAN-HOLDER PDF DISCOVERY")
    print("=" * 60)
    print(f"status       : {status.value if hasattr(status, 'value') else status}")
    print(f"pdfs found   : {meta.get('pdfs_found', '?')}")
    print(f"pdfs fetched : {meta.get('pdfs_fetched', '?')}")
    print(f"rows raw     : {meta.get('rows_raw', '?')}")
    print(f"records emit : {len(records)}")
    print(f"leads w/email: {len(leads)}")
    print(f"industry     : {args.industry} | location: {args.location}")
    print(f"elapsed      : {time.monotonic() - t0:.1f}s")
    print("-" * 60)
    for i, lead in enumerate(leads, 1):
        print(f"  [{i:>2}] {lead['email']:<45} {lead['company'][:35]}")
    print("=" * 60)

    if args.discover_only:
        print("Phase 1 complete (--discover-only). No AI research run.")
        return 0

    if status != SourceStatus.SUCCESS or not leads:
        print("No leads to research (discovery returned nothing usable).")
        return 1

    # Phase 2 — run the full AI research pipeline per lead.
    from app.lead_research.agent import AILeadResearchAgent

    agent = AILeadResearchAgent()
    print("\n" + "=" * 60)
    print("PHASE 2 - AI RESEARCH PIPELINE (per lead)")
    print("=" * 60)
    results = []
    for i, lead in enumerate(leads, 1):
        email, domain = lead["email"], lead["domain"]
        print(f"\n--- Lead {i}/{len(leads)}: {email} ({domain}) ---")
        lt0 = time.monotonic()
        try:
            dossier = agent.research(email, domain)
            elapsed = time.monotonic() - lt0
            results.append(
                {
                    "email": email,
                    "domain": domain,
                    "company": dossier.company.name,
                    "person": dossier.person.name,
                    "bound": dossier.person.bound,
                    "score": dossier.potential_score,
                    "recommendation": dossier.recommendation,
                    "intent": dossier.intent.needs_estimation if dossier.intent else "",
                    "timing": dossier.timing.window if dossier.timing else "",
                    "sources_checked": dossier.sources_checked,
                    "elapsed_s": round(elapsed, 1),
                }
            )
            print(
                f"  company={dossier.company.name[:40]!r} person={dossier.person.name!r} "
                f"bound={dossier.person.bound} score={dossier.potential_score} "
                f"rec={dossier.recommendation} [{elapsed:.0f}s]"
            )
            print(f"  sources={dossier.sources_checked}")
        except Exception as exc:  # noqa: BLE001 - one lead never kills the batch
            print(f"  ERROR: {exc}")
            results.append({"email": email, "domain": domain, "error": str(exc)})

    out = Path("output/plan_holder_research_results.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print("\n" + "=" * 60)
    print(f"Phase 2 complete: {len(results)} leads researched -> {out}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
