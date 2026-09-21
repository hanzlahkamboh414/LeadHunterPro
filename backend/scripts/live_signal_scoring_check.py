"""Recompute Phase 3 signals from real stored evidence/events and verify read-back."""

from __future__ import annotations

import argparse
import json
import sys

from app.research.signals import compute_company_signals
from app.research.store import ResearchEvidenceStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True)
    parser.add_argument("--db-path", default=None)
    args = parser.parse_args()

    store = ResearchEvidenceStore(args.db_path)
    company = store.companies_for_domain(args.domain)
    if company is None:
        print(json.dumps({"ok": False, "error": "company_not_found"}))
        return 2
    evidence = store.evidence_for_company(company.company_id)
    events = store.events_for_company(company.company_id)
    if not evidence or not events:
        print(json.dumps({
            "ok": False,
            "error": "stored_evidence_or_events_missing",
            "evidence": len(evidence),
            "events": len(events),
        }))
        return 2

    result = compute_company_signals(events, evidence)
    store.replace_signals(company.company_id, result.signals)
    read_back = store.signals_for_company(company.company_id)
    if len(read_back) != len(result.signals):
        print(json.dumps({"ok": False, "error": "signal_read_back_mismatch"}))
        return 3

    print(json.dumps({
        "ok": True,
        "company_id": company.company_id,
        "company_name": company.name,
        "evidence": len(evidence),
        "events": len(events),
        "signals": [item.to_row() for item in read_back],
        "contradictions": list(result.contradictions),
        "excluded_event_ids": list(result.excluded_event_ids),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
