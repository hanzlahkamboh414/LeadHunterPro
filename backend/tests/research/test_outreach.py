"""Phase 5 outreach trigger and Company Intelligence payload."""

from datetime import datetime, timezone

from app.research.events import EventRecord, new_event_id
from app.research.models import CanonicalEvidence, CompanyIdentity
from app.research.outreach import (
    OutreachStrength,
    build_company_intelligence,
    build_outreach_trigger,
)
from app.research.pain import (
    PainHypothesisRecord,
    PainType,
    PainVerdict,
    new_hypothesis_id,
)
from app.research.signals import (
    RecencyBucket,
    SignalRecord,
    SignalStrength,
    SignalType,
    new_signal_id,
)
from app.research.taxonomy import CompanyMatch, EventType, EvidenceType


COMPANY_ID = "cmp_acme"
NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _evidence(
    evidence_id: str = "ev_one",
    *,
    excerpt: str = "Our estimating team needs additional capacity.",
    published_at: str = "2026-09-10",
) -> CanonicalEvidence:
    return CanonicalEvidence(
        evidence_id=evidence_id,
        company_id=COMPANY_ID,
        source_url=f"https://acme.com/news/{evidence_id}",
        source_type="company_site",
        publisher="Acme Construction",
        title="Company update",
        excerpt=excerpt,
        published_at=published_at,
        retrieved_at="2026-09-21T00:00:00",
        company_match=CompanyMatch.CONFIRMED,
        evidence_type=EvidenceType.COMPANY_PAGE,
        confidence=0.95,
    )


def _event(evidence_id: str = "ev_one", *, occurred_at: str = "2026-09-10"):
    return EventRecord(
        event_id=new_event_id(
            COMPANY_ID, EventType.HIRING, "senior-estimator", occurred_at
        ),
        company_id=COMPANY_ID,
        event_type=EventType.HIRING,
        project_key="senior-estimator",
        occurred_at=occurred_at,
        confidence=0.95,
        evidence_ids=(evidence_id,),
    )


def _signal(
    evidence_id: str = "ev_one",
    *,
    recency: RecencyBucket = RecencyBucket.VERY_RECENT,
) -> SignalRecord:
    event = _event(evidence_id)
    return SignalRecord(
        signal_id=new_signal_id(COMPANY_ID, SignalType.ESTIMATING_ACTIVITY),
        company_id=COMPANY_ID,
        signal_type=SignalType.ESTIMATING_ACTIVITY,
        strength=SignalStrength.HIGH,
        score=12,
        computed_at="2026-09-21T00:00:00+00:00",
        contradiction=(),
        evidence_ids=(evidence_id,),
        event_ids=(event.event_id,),
        event_count=1,
        independent_source_count=1,
        recency=recency,
    )


def _pain(
    verdict: PainVerdict,
    *,
    direct: bool = False,
    confidence: float = 0.8,
) -> PainHypothesisRecord:
    return PainHypothesisRecord(
        hypothesis_id=new_hypothesis_id(COMPANY_ID, PainType.ESTIMATING_CAPACITY),
        company_id=COMPANY_ID,
        pain_type=PainType.ESTIMATING_CAPACITY,
        confidence=confidence,
        licensed_confidence=0.85,
        verdict=verdict,
        blocked_by=() if verdict is PainVerdict.VERIFIED else ("more proof needed",),
        computed_at="2026-09-21T00:00:00+00:00",
        reasoning="Stored evidence suggests estimating capacity demand.",
        evidence_ids=("ev_one",),
        signal_ids=(new_signal_id(COMPANY_ID, SignalType.ESTIMATING_ACTIVITY),),
        direct_evidence_ids=("ev_one",) if direct else (),
    )


def test_direct_verified_pain_gets_confirmed_safe_wording():
    trigger = build_outreach_trigger(
        CompanyIdentity(COMPANY_ID, name="Acme Construction"),
        [_pain(PainVerdict.VERIFIED, direct=True)],
    )

    assert trigger.strength is OutreachStrength.CONFIRMED
    assert trigger.angle == "Additional Estimating Capacity"
    assert "recently highlighted estimating capacity needs" in trigger.wording
    assert "overloaded" not in trigger.wording.lower()
    assert trigger.evidence_ids == ("ev_one",)


def test_likely_pain_uses_weak_inference_language():
    trigger = build_outreach_trigger(
        CompanyIdentity(COMPANY_ID, name="Acme Construction"),
        [_pain(PainVerdict.LIKELY, confidence=0.6)],
    )

    assert trigger.strength is OutreachStrength.WEAK_INFERENCE
    assert trigger.wording.startswith("As estimating activity picks up")
    assert "I know" not in trigger.wording


def test_blocked_or_unknown_pain_uses_generic_no_evidence_wording():
    trigger = build_outreach_trigger(
        CompanyIdentity(COMPANY_ID, name="Acme Construction"),
        [_pain(PainVerdict.BLOCKED)],
    )

    assert trigger.strength is OutreachStrength.NONE
    assert trigger.angle == "General Estimating Support"
    assert trigger.wording == (
        "We support contractors when additional estimating capacity is needed."
    )
    assert trigger.evidence_ids == ()


def test_company_intelligence_has_recent_activity_and_every_why_source():
    company = CompanyIdentity(COMPANY_ID, name="Acme Construction")
    evidence = _evidence()
    event = _event()
    signal = _signal()
    pain = _pain(PainVerdict.VERIFIED, direct=True)
    payload = build_company_intelligence(
        company,
        [event],
        [signal],
        [pain],
        [evidence],
        coverage={"company_site": "VERIFIED"},
        now=NOW,
    )

    assert payload["recent_activity"][0]["event_type"] == "hiring"
    assert payload["signals"][0]["strength"] == "HIGH"
    hypothesis = payload["pain_hypotheses"][0]
    assert hypothesis["why"][0] == {
        "evidence_id": "ev_one",
        "source_url": "https://acme.com/news/ev_one",
        "source_type": "company_site",
        "publisher": "Acme Construction",
        "published_at": "2026-09-10",
        "title": "Company update",
        "excerpt": "Our estimating team needs additional capacity.",
    }
    assert payload["recommended_angle"] == "Additional Estimating Capacity"
    assert payload["coverage"] == {"company_site": "VERIFIED"}


def test_historical_event_is_not_rendered_as_recent_activity():
    payload = build_company_intelligence(
        CompanyIdentity(COMPANY_ID, name="Acme Construction"),
        [_event(occurred_at="2024-01-01")],
        [_signal(recency=RecencyBucket.BACKGROUND)],
        [_pain(PainVerdict.BLOCKED)],
        [_evidence(published_at="2024-01-01")],
        now=NOW,
    )

    assert payload["recent_activity"] == []
    assert payload["outreach_trigger"]["strength"] == "none"
