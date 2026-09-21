"""Build and persist a real Phase 5 Company Intelligence snapshot."""

from __future__ import annotations

import argparse
import json

from app.research.outreach import build_company_intelligence, build_outreach_trigger
from app.research.store import ResearchEvidenceStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="turnerconstruction.com")
    args = parser.parse_args()

    store = ResearchEvidenceStore()
    company = store.companies_for_domain(args.domain)
    if company is None:
        raise SystemExit(f"No stored company found for {args.domain}")
    events = store.events_for_company(company.company_id)
    signals = store.signals_for_company(company.company_id)
    pains = store.pain_hypotheses_for_company(company.company_id)
    evidence = store.evidence_for_company(company.company_id)
    trigger = build_outreach_trigger(company, pains)
    store.replace_outreach_trigger(company.company_id, trigger)
    payload = build_company_intelligence(
        company, events, signals, pains, evidence,
        coverage=store.latest_coverage_for_company(company.company_id),
    )
    stored = store.outreach_trigger_for_company(company.company_id)
    if stored != trigger:
        raise SystemExit("Stored outreach trigger did not round-trip")
    if "overloaded" in trigger.wording.lower():
        raise SystemExit("Unsafe overload claim reached outreach")
    print(json.dumps({
        "company": company.name,
        "company_id": company.company_id,
        "evidence": len(evidence),
        "events": len(events),
        "recent_activity": len(payload["recent_activity"]),
        "signals": len(signals),
        "pain_hypotheses": [
            {"type": item.pain_type.value, "verdict": item.verdict.value}
            for item in pains
        ],
        "recommended_angle": trigger.angle,
        "trigger_strength": trigger.strength.value,
        "safe_wording": trigger.wording,
        "stored": True,
    }, indent=2))


if __name__ == "__main__":
    main()
