"""Convert gated company pain into safe, deterministic outreach copy."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from app.research.events import EventRecord
from app.research.models import CanonicalEvidence, CompanyIdentity
from app.research.outreach.models import (
    OutreachStrength,
    OutreachTriggerRecord,
    new_trigger_id,
)
from app.research.pain import PainHypothesisRecord, PainType, PainVerdict
from app.research.signals import SignalRecord, recency_bucket


_GENERIC = "We support contractors when additional estimating capacity is needed."
_ANGLES = {
    PainType.ESTIMATING_CAPACITY: "Additional Estimating Capacity",
    PainType.BID_DEADLINE_PRESSURE: "Bid Deadline Support",
    PainType.BID_VOLUME: "Bid Volume Support",
    PainType.PRECONSTRUCTION_WORKLOAD: "Preconstruction Support",
    PainType.HIRING_STAFFING_CAPACITY: "Flexible Estimating Capacity",
    PainType.EXPANSION_SCALING: "Scalable Estimating Support",
    PainType.PROJECT_VOLUME: "Project Estimating Support",
    PainType.SCOPE_TAKEOFF_WORKLOAD: "Takeoff Support",
}
_WORDING = {
    PainType.ESTIMATING_CAPACITY: (
        "I saw that {company} recently highlighted estimating capacity needs.",
        "With estimating needs becoming visible at {company}, additional capacity may help.",
        "As estimating activity picks up, flexible capacity can help protect deadlines.",
    ),
    PainType.BID_DEADLINE_PRESSURE: (
        "I saw that {company} recently highlighted bid deadline pressure.",
        "With several bids moving forward at {company}, extra estimating support may help.",
        "As bid deadlines pick up, flexible estimating support can help.",
    ),
    PainType.BID_VOLUME: (
        "I saw that {company} recently reported increased bid activity.",
        "With several bids moving forward at {company}, extra estimating capacity may help.",
        "As bid volume picks up, flexible estimating support can help.",
    ),
    PainType.PRECONSTRUCTION_WORKLOAD: (
        "I saw that {company} recently highlighted preconstruction activity.",
        "With several projects moving through preconstruction at {company}, extra support may help.",
        "As preconstruction activity picks up, flexible estimating support can help.",
    ),
    PainType.HIRING_STAFFING_CAPACITY: (
        "I saw that {company} recently advertised estimating team capacity needs.",
        "With estimating hiring activity at {company}, flexible outside capacity may help.",
        "As hiring activity picks up, flexible estimating support can help.",
    ),
    PainType.EXPANSION_SCALING: (
        "I saw that {company} recently announced expansion activity.",
        "With expansion activity at {company}, scalable estimating capacity may help.",
        "As expansion activity picks up, flexible estimating support can help.",
    ),
    PainType.PROJECT_VOLUME: (
        "I saw that {company} recently announced new project activity.",
        "With several projects moving forward at {company}, extra estimating support may help.",
        "As project activity picks up, flexible estimating support can help.",
    ),
    PainType.SCOPE_TAKEOFF_WORKLOAD: (
        "I saw that {company} recently highlighted takeoff activity.",
        "With scope and takeoff activity at {company}, extra capacity may help.",
        "As takeoff activity picks up, flexible estimating support can help.",
    ),
}


def _now(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def build_outreach_trigger(
    company: CompanyIdentity,
    pains: Sequence[PainHypothesisRecord],
    *,
    now: datetime | None = None,
) -> OutreachTriggerRecord:
    eligible = [p for p in pains if p.verdict in {PainVerdict.VERIFIED, PainVerdict.LIKELY}]
    eligible.sort(
        key=lambda p: (
            0 if p.verdict is PainVerdict.VERIFIED else 1,
            -p.licensed_confidence,
            p.pain_type.value,
        )
    )
    if not eligible:
        return OutreachTriggerRecord(
            new_trigger_id(company.company_id), company.company_id,
            "General Estimating Support", "No licensed current pain",
            OutreachStrength.NONE, _GENERIC, _now(now), (),
        )

    pain = eligible[0]
    direct = pain.verdict is PainVerdict.VERIFIED and bool(pain.direct_evidence_ids)
    if direct:
        strength, index = OutreachStrength.CONFIRMED, 0
        evidence_ids = pain.direct_evidence_ids
    elif pain.verdict is PainVerdict.VERIFIED:
        strength, index = OutreachStrength.STRONG_INFERENCE, 1
        evidence_ids = pain.evidence_ids
    else:
        strength, index = OutreachStrength.WEAK_INFERENCE, 2
        evidence_ids = pain.evidence_ids
    company_name = (company.name or company.company_key or "your team").strip()
    return OutreachTriggerRecord(
        new_trigger_id(company.company_id), company.company_id,
        _ANGLES[pain.pain_type], pain.pain_type.value, strength,
        _WORDING[pain.pain_type][index].format(company=company_name),
        _now(now), evidence_ids,
    )


def build_company_intelligence(
    company: CompanyIdentity,
    events: Sequence[EventRecord],
    signals: Sequence[SignalRecord],
    pains: Sequence[PainHypothesisRecord],
    evidence: Sequence[CanonicalEvidence],
    *,
    coverage: dict | None = None,
    now: datetime | None = None,
) -> dict:
    current = now or datetime.now(timezone.utc)
    evidence_by_id = {item.evidence_id: item for item in evidence}
    trigger = build_outreach_trigger(company, pains, now=current)

    recent_activity = []
    for event in events:
        bucket = recency_bucket(event.occurred_at, now=current)
        if bucket.value not in {"very_recent", "recent"}:
            continue
        recent_activity.append({
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "occurred_at": event.occurred_at,
            "confidence": event.confidence,
            "recency": bucket.value,
            "evidence_ids": list(event.evidence_ids),
        })

    pain_rows = []
    for pain in pains:
        why = []
        for evidence_id in pain.evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if item is None:
                continue
            why.append({
                "evidence_id": item.evidence_id,
                "source_url": item.source_url,
                "source_type": item.source_type,
                "publisher": item.publisher,
                "published_at": item.published_at,
                "title": item.title,
                "excerpt": item.excerpt,
            })
        pain_rows.append({
            "hypothesis_id": pain.hypothesis_id,
            "pain_type": pain.pain_type.value,
            "verdict": pain.verdict.value,
            "confidence": pain.licensed_confidence,
            "reasoning": pain.reasoning,
            "blocked_by": list(pain.blocked_by),
            "why": why,
        })

    return {
        "company_id": company.company_id,
        "company_name": company.name,
        "recent_activity": recent_activity,
        "signals": [item.to_row() for item in signals],
        "pain_hypotheses": pain_rows,
        "recommended_angle": trigger.angle,
        "outreach_trigger": trigger.to_dict(),
        "coverage": dict(coverage or {}),
    }
