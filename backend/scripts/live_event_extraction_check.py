"""Live Phase 2 gate: stored canonical evidence -> AI -> stored events."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.gateway import make_ai_ask  # noqa: E402
from app.research.events import extract_company_events  # noqa: E402
from app.research.store import ResearchEvidenceStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    store = ResearchEvidenceStore(args.db)
    company = store.companies_for_domain(args.domain)
    if company is None:
        print(json.dumps({"ok": False, "reason": "company_not_found"}))
        return 2

    existing = store.events_for_company(company.company_id)
    covered = {
        evidence_id for event in existing for evidence_id in event.evidence_ids
    }
    evidence = [
        item for item in store.evidence_for_company(company.company_id)
        if item.evidence_id not in covered
    ]
    if not evidence:
        print(json.dumps({
            "ok": bool(existing),
            "company_id": company.company_id,
            "pending_evidence": 0,
            "existing_events": len(existing),
        }, sort_keys=True))
        return 0 if existing else 2

    result = extract_company_events(
        evidence, company_name=company.name, ai_ask=make_ai_ask()
    )
    stored = sum(1 for event in result.events if store.add_event(event))
    output = {
        "ok": bool(result.events),
        "company_id": company.company_id,
        "evidence_read": len(evidence),
        "events_accepted": len(result.events),
        "events_stored": stored,
        "events_total": len(store.events_for_company(company.company_id)),
        "rejections": list(result.rejections),
    }
    print(json.dumps(output, sort_keys=True))
    return 0 if output["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
