"""Phase 4 candidate-pain AI call and deterministic PAIN_GATE."""

from app.research.events import EventRecord, new_event_id
from app.research.models import CanonicalEvidence
from app.research.pain import (
    PainType,
    PainVerdict,
    infer_company_pain,
    pain_basis_hash,
)
from app.research.signals import (
    RecencyBucket,
    SignalRecord,
    SignalStrength,
    SignalType,
    new_signal_id,
)
from app.research.taxonomy import CompanyMatch, EventType, EvidenceType


COMPANY = "cmp_acme"


def _evidence(
    evidence_id: str,
    *,
    publisher: str,
    excerpt: str,
    company_match: CompanyMatch = CompanyMatch.CONFIRMED,
    confidence: float = 0.95,
) -> CanonicalEvidence:
    return CanonicalEvidence(
        evidence_id=evidence_id,
        company_id=COMPANY,
        source_url=f"https://{publisher.lower().replace(' ', '')}.example/{evidence_id}",
        retrieved_at="2026-09-21T00:00:00",
        publisher=publisher,
        title="Stored source record",
        excerpt=excerpt,
        published_at="2026-09-10",
        company_match=company_match,
        evidence_type=EvidenceType.COMPANY_PAGE,
        confidence=confidence,
    )


def _event(event_type: EventType, evidence_id: str, *, key: str) -> EventRecord:
    return EventRecord(
        event_id=new_event_id(COMPANY, event_type, key, "2026-09-10"),
        company_id=COMPANY,
        event_type=event_type,
        project_key=key,
        occurred_at="2026-09-10",
        confidence=0.95,
        evidence_ids=(evidence_id,),
    )


def _signal(
    signal_type: SignalType,
    evidence_id: str,
    event_id: str,
    *,
    strength: SignalStrength = SignalStrength.HIGH,
    recency: RecencyBucket = RecencyBucket.VERY_RECENT,
    contradiction: tuple[str, ...] = (),
) -> SignalRecord:
    return SignalRecord(
        signal_id=new_signal_id(COMPANY, signal_type),
        company_id=COMPANY,
        signal_type=signal_type,
        strength=strength,
        score=12 if strength is SignalStrength.HIGH else 8,
        computed_at="2026-09-21T00:00:00+00:00",
        contradiction=contradiction,
        evidence_ids=(evidence_id,),
        event_ids=(event_id,),
        event_count=1,
        independent_source_count=1,
        recency=recency,
    )


def _proposal(
    pain_type: str,
    signal_ids: list[str],
    evidence_ids: list[str],
    *,
    confidence: float = 0.8,
) -> str:
    import json

    return json.dumps({
        "candidate_signals": [
            {"signal_id": value, "reasoning": "stored signal supports correlation"}
            for value in signal_ids
        ],
        "candidate_pain_hypotheses": [{
            "pain_type": pain_type,
            "confidence": confidence,
            "signal_ids": signal_ids,
            "evidence_ids": evidence_ids,
            "reasoning": "Correlation proposed from stored events and signals.",
        }],
    })


def _two_signal_case():
    job_evidence = _evidence(
        "ev_job", publisher="Acme Careers", excerpt="Senior Estimator position open."
    )
    award_evidence = _evidence(
        "ev_award", publisher="City of Austin", excerpt="Contract awarded to Acme."
    )
    job_event = _event(EventType.HIRING, "ev_job", key="senior-estimator")
    award_event = _event(EventType.CONTRACT_AWARDED, "ev_award", key="school")
    estimating = _signal(
        SignalType.ESTIMATING_ACTIVITY, "ev_job", job_event.event_id
    )
    projects = _signal(
        SignalType.PROJECT_ACTIVITY, "ev_award", award_event.event_id
    )
    return (
        [job_event, award_event],
        [estimating, projects],
        [job_evidence, award_evidence],
    )


def test_two_independent_recent_strong_signals_verify_estimating_capacity():
    events, signals, evidence = _two_signal_case()
    raw = _proposal(
        PainType.ESTIMATING_CAPACITY.value,
        [item.signal_id for item in signals],
        [item.evidence_ids[0] for item in signals],
    )
    result = infer_company_pain(events, signals, evidence, ai_ask=lambda _: raw)

    assert len(result.hypotheses) == 1
    hypothesis = result.hypotheses[0]
    assert hypothesis.verdict is PainVerdict.VERIFIED
    assert hypothesis.blocked_by == ()


def test_three_awards_alone_never_become_estimating_capacity():
    evidence = [
        _evidence(f"ev_{i}", publisher="USAspending", excerpt="Contract awarded.")
        for i in range(3)
    ]
    events = [
        _event(EventType.CONTRACT_AWARDED, item.evidence_id, key=f"project-{i}")
        for i, item in enumerate(evidence)
    ]
    signal = SignalRecord(
        signal_id=new_signal_id(COMPANY, SignalType.CONTRACT_AWARD_ACTIVITY),
        company_id=COMPANY,
        signal_type=SignalType.CONTRACT_AWARD_ACTIVITY,
        strength=SignalStrength.HIGH,
        score=15,
        computed_at="2026-09-21T00:00:00+00:00",
        contradiction=(),
        evidence_ids=tuple(item.evidence_id for item in evidence),
        event_ids=tuple(item.event_id for item in events),
        event_count=3,
        independent_source_count=1,
        recency=RecencyBucket.VERY_RECENT,
    )
    raw = _proposal(
        PainType.ESTIMATING_CAPACITY.value,
        [signal.signal_id],
        [item.evidence_id for item in evidence],
        confidence=0.3,
    )
    hypothesis = infer_company_pain(
        events, [signal], evidence, ai_ask=lambda _: raw
    ).hypotheses[0]

    assert hypothesis.verdict is PainVerdict.UNKNOWN
    assert any("estimating_activity" in item for item in hypothesis.blocked_by)


def test_explicit_direct_pain_language_can_verify_with_one_signal():
    evidence = _evidence(
        "ev_direct",
        publisher="Acme",
        excerpt="Our estimating team is overloaded and needs additional capacity.",
    )
    event = _event(EventType.HIRING, evidence.evidence_id, key="estimating-team")
    signal = _signal(
        SignalType.ESTIMATING_ACTIVITY, evidence.evidence_id, event.event_id,
        strength=SignalStrength.MEDIUM,
    )
    raw = _proposal(
        PainType.ESTIMATING_CAPACITY.value,
        [signal.signal_id],
        [evidence.evidence_id],
        confidence=0.85,
    )
    hypothesis = infer_company_pain(
        [event], [signal], [evidence], ai_ask=lambda _: raw
    ).hypotheses[0]

    assert hypothesis.verdict is PainVerdict.VERIFIED
    assert hypothesis.direct_evidence_ids == (evidence.evidence_id,)


def test_unconfirmed_company_identity_blocks_the_candidate():
    events, signals, evidence = _two_signal_case()
    evidence[0] = _evidence(
        "ev_job", publisher="Acme Careers", excerpt="Estimator opening.",
        company_match=CompanyMatch.INFERRED,
    )
    raw = _proposal(
        PainType.ESTIMATING_CAPACITY.value,
        [item.signal_id for item in signals],
        [item.evidence_ids[0] for item in signals],
    )
    hypothesis = infer_company_pain(
        events, signals, evidence, ai_ask=lambda _: raw
    ).hypotheses[0]

    assert hypothesis.verdict is PainVerdict.BLOCKED
    assert any("company identity" in item for item in hypothesis.blocked_by)


def test_a_contradicted_signal_blocks_the_candidate():
    events, signals, evidence = _two_signal_case()
    signals[0] = _signal(
        SignalType.ESTIMATING_ACTIVITY,
        "ev_job",
        events[0].event_id,
        contradiction=("position was later filled",),
    )
    raw = _proposal(
        PainType.ESTIMATING_CAPACITY.value,
        [item.signal_id for item in signals],
        [item.evidence_ids[0] for item in signals],
    )
    hypothesis = infer_company_pain(
        events, signals, evidence, ai_ask=lambda _: raw
    ).hypotheses[0]

    assert hypothesis.verdict is PainVerdict.BLOCKED
    assert any("contradiction" in item for item in hypothesis.blocked_by)


def test_stale_support_is_unknown_not_recent():
    events, signals, evidence = _two_signal_case()
    signals = [
        _signal(
            item.signal_type, item.evidence_ids[0], item.event_ids[0],
            recency=RecencyBucket.BACKGROUND,
        )
        for item in signals
    ]
    raw = _proposal(
        PainType.ESTIMATING_CAPACITY.value,
        [item.signal_id for item in signals],
        [item.evidence_ids[0] for item in signals],
        confidence=0.3,
    )
    hypothesis = infer_company_pain(
        events, signals, evidence, ai_ask=lambda _: raw
    ).hypotheses[0]

    assert hypothesis.verdict is PainVerdict.UNKNOWN
    assert any("recent" in item for item in hypothesis.blocked_by)


def test_overconfident_candidate_is_downgraded_to_likely():
    events, signals, evidence = _two_signal_case()
    raw = _proposal(
        PainType.ESTIMATING_CAPACITY.value,
        [item.signal_id for item in signals],
        [item.evidence_ids[0] for item in signals],
        confidence=0.95,
    )
    hypothesis = infer_company_pain(
        events, signals, evidence, ai_ask=lambda _: raw
    ).hypotheses[0]

    assert hypothesis.verdict is PainVerdict.LIKELY
    assert hypothesis.licensed_confidence == 0.85
    assert any("confidence" in item for item in hypothesis.blocked_by)


def test_unknown_references_are_blocked_and_never_silently_dropped():
    events, signals, evidence = _two_signal_case()
    raw = _proposal(
        PainType.ESTIMATING_CAPACITY.value,
        ["sig_invented"],
        ["ev_invented"],
    )
    hypothesis = infer_company_pain(
        events, signals, evidence, ai_ask=lambda _: raw
    ).hypotheses[0]

    assert hypothesis.verdict is PainVerdict.BLOCKED
    assert any("unknown signal_id" in item for item in hypothesis.blocked_by)
    assert any("unknown evidence_id" in item for item in hypothesis.blocked_by)


def test_ai_is_called_once_and_prompt_forbids_researching_the_web():
    events, signals, evidence = _two_signal_case()
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return '{"candidate_signals":[],"candidate_pain_hypotheses":[]}'

    result = infer_company_pain(events, signals, evidence, ai_ask=ask)

    assert result.hypotheses == ()
    assert len(prompts) == 1
    assert "MUST NOT search" in prompts[0]
    assert "canonical excerpts are not available" in prompts[0]


def test_unknown_pain_type_is_rejected():
    events, signals, evidence = _two_signal_case()
    raw = _proposal(
        "made_up_pain",
        [item.signal_id for item in signals],
        [item.evidence_ids[0] for item in signals],
    )
    result = infer_company_pain(events, signals, evidence, ai_ask=lambda _: raw)

    assert result.hypotheses == ()
    assert "unknown pain_type" in result.rejections[0]


def test_basis_hash_ignores_recompute_timestamp_but_tracks_factual_changes():
    events, signals, _ = _two_signal_case()
    original = pain_basis_hash(events, signals)
    row = signals[0]
    recomputed = SignalRecord(
        signal_id=row.signal_id,
        company_id=row.company_id,
        signal_type=row.signal_type,
        strength=row.strength,
        score=row.score,
        computed_at="2026-09-22T00:00:00+00:00",
        contradiction=row.contradiction,
        evidence_ids=row.evidence_ids,
        event_ids=row.event_ids,
        event_count=row.event_count,
        independent_source_count=row.independent_source_count,
        recency=row.recency,
    )
    assert pain_basis_hash(events, [recomputed, signals[1]]) == original

    stronger = SignalRecord(
        **{**recomputed.__dict__, "score": recomputed.score + 1}
    )
    assert pain_basis_hash(events, [stronger, signals[1]]) != original
