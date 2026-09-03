"""Inc 7 — measure real plan-holder pipeline impact end-to-end (no fabrication).

Reads real plan-holder company records (connector schema, exactly as
``PlanHolderSource._to_company_record`` emits them) from a JSON file and runs
each through the REAL production pipeline, then reports the honest coverage /
qualification metrics.

The path is the same one ``run_leads.py`` drives, but scoped to plan-holder
records so the plan-holder pipeline's impact is measured on its own:

    plan-holder record dict
      -> TexasProcurementConnector._verify_company   (real AcceptanceGate)
      -> _build_result + verification metadata merge (real Step-4 projection)
      -> run_leads.to_company_discovery              (real bridge)
      -> LeadPipeline.qualify_company                (REAL leadership crawl for
                                                      role, REAL intent plugins,
                                                      REAL CompanyScorer)
      -> Lead.qualification_gate()                   (the V1 verdict)

NO record is manually altered to qualify. NO missing evidence is fabricated.
``person_bound`` emails are preserved verbatim. Hard rejections and the
Tier-4 cap are left intact (they come from the real gate). The V1 gate is
never weakened.

Usage:
    python scripts/measure_plan_holder_impact.py records.json [industry] [location]

Where ``records.json`` is a list of plan-holder company record dicts (the
shape emitted by ``PlanHolderSource`` / ``PdfPlanHolderParser`` + projection).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.connectors.texas_procurement import TexasProcurementConnector
from app.engines.lead.lead_pipeline import LeadPipeline
from app.engines.lead.lead_models import IntentEvidenceType
from run_leads import to_company_discovery

#: V1 gate thresholds (mirrors lead_models.QUALIFIED_THRESHOLD).
QUALIFIED_THRESHOLD = 90


def _record_plan_holder_stats(record: dict[str, Any]) -> dict[str, bool]:
    """Per-record documentary-evidence flags, read ONLY from the record."""
    block = record.get("plan_holder") or {}
    person = block.get("person") or {}
    emails = block.get("emails") or []
    has_person = bool((person.get("name") or "").strip())
    has_person_bound = any(e.get("tier") == "person_bound" for e in emails)
    source_url = str(person.get("source_url") or "").strip() or str(
        record.get("source_url") or ""
    ).strip()
    return {
        "named_person": has_person,
        "person_bound_email": has_person_bound,
        "both_person_and_email": has_person and has_person_bound,
        "valid_public_source_url": bool(source_url),
        # plan_holder_confirmed requires ALL: block + person + person_bound
        # email + source_url (the same rule AcceptanceGate enforces).
        "complete": (
            has_person and has_person_bound and bool(source_url)
        ),
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/measure_plan_holder_impact.py records.json [industry] [location]")
        return 2
    records_path = Path(sys.argv[1])
    records = json.loads(records_path.read_text(encoding="utf-8"))
    industry = sys.argv[2] if len(sys.argv) > 2 else "Roofing"
    location = sys.argv[3] if len(sys.argv) > 3 else "Dallas Texas"

    conn = TexasProcurementConnector()
    pipe = LeadPipeline()  # REAL leadership / intent / scorer (live seams)

    # --- 1. documentary evidence stats (pre-gate, honest) ---
    stats = {"total_records": len(records)}
    flags = {"named_person": 0, "person_bound_email": 0, "both_person_and_email": 0,
             "valid_public_source_url": 0, "complete": 0}
    for rec in records:
        for k, v in _record_plan_holder_stats(rec).items():
            flags[k] += int(v)
    stats["record_flags"] = flags

    # --- 2. run the real pipeline ---
    verified = []
    plan_holder_confirmed = 0
    partially_verified = 0
    gate_accepted = 0
    role_passed = 0
    relevant_role = 0
    intent_valid = 0
    ai_ok = 0
    qualified = 0
    rule_blockers = Counter()
    all_blockers: list[tuple[str, list[str]]] = []

    for rec in records:
        name = rec.get("company_name", "?")
        gate = conn._verify_company(rec, location)
        ph = gate.plan_holder_confirmed
        plan_holder_confirmed += int(ph)
        partially_verified += int(gate.verification_status.value == "partially_verified")
        gate_accepted += int(gate.accepted)

        # Real Step-4 projection: build result + merge verification metadata.
        result, _ = conn._build_result(rec, industry, set())
        result = replace(result, metadata={
            **result.metadata,
            "verification_status": gate.verification_status.value,
            "verification_confidence": gate.verification_confidence,
            "source_tier": gate.source_tier,
            "gate_accepted": gate.accepted,
            "verification": gate.to_dict(),
        })
        company = to_company_discovery(result)

        # Real pipeline (live leadership crawl for role + intent + AI).
        try:
            lead = pipe.qualify_company(company, {"industry": industry, "location": location})
        except Exception as exc:  # noqa: BLE001 — a stage failure is honest, not fatal
            all_blockers.append((name, [f"pipeline error: {exc}"]))
            rule_blockers["pipeline error"] += 1
            continue

        person = lead.person
        role_relevant = bool(person and person.role_relevance)
        role_passed += int(role_relevant)
        relevant_role += int(role_relevant)
        intent_valid += int(lead.has_intent_signal)
        ai_ok += int(lead.ai.ai_confidence >= QUALIFIED_THRESHOLD and bool(lead.ai.justification))

        blockers = lead.qualification_gate().blocked_by
        all_blockers.append((name, blockers))
        if not blockers:
            qualified += 1
        else:
            rule_blockers[blockers[0]] += 1

    # --- 3. summary ---
    print("=" * 72)
    print("PLAN-HOLDER PIPELINE — REAL LIVE IMPACT (no fabrication)")
    print(f"query: {industry} / {location}")
    print("=" * 72)
    print(f"1.  total plan-holder records       : {stats['total_records']}")
    f = stats["record_flags"]
    print(f"2.  with named person               : {f['named_person']}")
    print(f"3.  with person_bound email         : {f['person_bound_email']}")
    print(f"4.  named person + person_bound     : {f['both_person_and_email']}")
    print(f"5.  valid public source_url         : {f['valid_public_source_url']}")
    print(f"6.  plan_holder_confirmed (gate)    : {plan_holder_confirmed}")
    print(f"7.  reaching partially_verified     : {partially_verified}")
    print(f"8.  gate_accepted                   : {gate_accepted}")
    print(f"9.  role_relevant (post-enrichment) : {role_passed}")
    print(f"10. valid intent evidence           : {intent_valid}")
    print(f"11. AI requirements met (>=90+just) : {ai_ok}")
    print(f"12. FINAL QUALIFIED LEADS           : {qualified}")
    print("\n13. Blocked per first-rule:")
    for rule, count in rule_blockers.most_common():
        print(f"      {count:>3}  {rule}")
    print(f"\n14. Per-record blocker detail ({len(all_blockers)}):")
    for name, blockers in all_blockers:
        tag = "QUALIFIED" if not blockers else ("; ".join(blockers)[:110])
        print(f"      {name:<30} -> {tag}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
