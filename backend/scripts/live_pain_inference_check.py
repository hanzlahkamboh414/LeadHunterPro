"""Run real AI Call #2 on stored company intelligence and verify gated read-back."""

from __future__ import annotations

import argparse
import json
import sys

from app.ai.gateway import make_ai_ask
from app.research.pain import infer_company_pain, pain_basis_hash
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
    events = store.events_for_company(company.company_id)
    signals = store.signals_for_company(company.company_id)
    evidence = store.evidence_for_company(company.company_id)
    if not events or not signals or not evidence:
        print(json.dumps({
            "ok": False,
            "error": "stored_intelligence_incomplete",
            "events": len(events),
            "signals": len(signals),
            "evidence": len(evidence),
        }))
        return 2

    result = infer_company_pain(
        events, signals, evidence, ai_ask=make_ai_ask()
    )
    basis = pain_basis_hash(events, signals)
    store.replace_pain_hypotheses(
        company.company_id, result.hypotheses, basis_hash=basis
    )
    read_back = store.pain_hypotheses_for_company(company.company_id)
    if read_back != list(result.hypotheses):
        print(json.dumps({"ok": False, "error": "pain_read_back_mismatch"}))
        return 3
    recent_signal_exists = any(
        item.recency.value in {"very_recent", "recent"} for item in signals
    )
    if not recent_signal_exists and any(
        item.verdict.value == "VERIFIED" for item in read_back
    ):
        print(json.dumps({"ok": False, "error": "stale_pain_was_verified"}))
        return 4

    print(json.dumps({
        "ok": True,
        "company_id": company.company_id,
        "company_name": company.name,
        "events": len(events),
        "signals": len(signals),
        "evidence": len(evidence),
        "hypotheses": [item.to_row() for item in read_back],
        "rejections": list(result.rejections),
        "basis_hash": basis,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
