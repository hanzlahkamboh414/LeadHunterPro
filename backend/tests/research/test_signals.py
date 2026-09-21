"""Phase 3 deterministic signal-engine behavior."""

from datetime import datetime, timezone

from app.research.events import EventRecord, new_event_id
from app.research.models import CanonicalEvidence
from app.research.signals import (
    RecencyBucket,
    SignalStrength,
    SignalType,
    compute_company_signals,
    recency_bucket,
)
from app.research.taxonomy import CompanyMatch, EventType, EvidenceType


NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _evidence(
    evidence_id: str,
    *,
    company_id: str = "cmp_acme",
    source_url: str = "https://city.gov/award/1",
    publisher: str = "City of Austin",
    excerpt: str = "Contract awarded to Acme.",
    published_at: str = "2026-09-10",
    evidence_type: EvidenceType = EvidenceType.GOVERNMENT_AWARD,
) -> CanonicalEvidence:
    return CanonicalEvidence(
        evidence_id=evidence_id,
        company_id=company_id,
        source_url=source_url,
        source_type="test",
        publisher=publisher,
        title="Official record",
        excerpt=excerpt,
        published_at=published_at,
        retrieved_at="2026-09-21T00:00:00",
        company_match=CompanyMatch.CONFIRMED,
        evidence_type=evidence_type,
        confidence=0.95,
    )


def _event(
    event_type: EventType,
    evidence_ids: tuple[str, ...],
    *,
    company_id: str = "cmp_acme",
    project_key: str = "project-one",
    occurred_at: str = "2026-09-10",
) -> EventRecord:
    return EventRecord(
        event_id=new_event_id(company_id, event_type, project_key, occurred_at),
        company_id=company_id,
        event_type=event_type,
        project_key=project_key,
        occurred_at=occurred_at,
        confidence=0.95,
        evidence_ids=evidence_ids,
    )


def test_recency_buckets_pin_every_boundary_and_unknown_date():
    assert recency_bucket("2026-09-21", now=NOW) is RecencyBucket.VERY_RECENT
    assert recency_bucket("2026-08-22", now=NOW) is RecencyBucket.VERY_RECENT
    assert recency_bucket("2026-08-21", now=NOW) is RecencyBucket.RECENT
    assert recency_bucket("2026-06-23", now=NOW) is RecencyBucket.RECENT
    assert recency_bucket("2026-06-22", now=NOW) is RecencyBucket.MODERATE
    assert recency_bucket("2026-03-25", now=NOW) is RecencyBucket.MODERATE
    assert recency_bucket("2026-03-24", now=NOW) is RecencyBucket.HISTORICAL
    assert recency_bucket("2025-09-21", now=NOW) is RecencyBucket.HISTORICAL
    assert recency_bucket("2025-09-20", now=NOW) is RecencyBucket.BACKGROUND
    assert recency_bucket("", now=NOW) is RecencyBucket.BACKGROUND
    assert recency_bucket("not-a-date", now=NOW) is RecencyBucket.BACKGROUND


def test_award_produces_activity_and_award_signals_but_not_pain():
    evidence = [_evidence("ev_award")]
    event = _event(EventType.CONTRACT_AWARDED, ("ev_award",))
    result = compute_company_signals([event], evidence, now=NOW)

    types = {signal.signal_type for signal in result.signals}
    assert SignalType.RECENT_ACTIVITY in types
    assert SignalType.CONTRACT_AWARD_ACTIVITY in types
    assert SignalType.PROJECT_ACTIVITY in types
    assert all("pain" not in signal.signal_type.value for signal in result.signals)


def test_permit_is_project_activity_and_never_award_or_bid_activity():
    evidence = [
        _evidence(
            "ev_permit",
            excerpt="Permit issued for Project One.",
            evidence_type=EvidenceType.PERMIT_RECORD,
        )
    ]
    event = _event(EventType.PERMIT_ISSUED, ("ev_permit",))
    result = compute_company_signals([event], evidence, now=NOW)
    types = {signal.signal_type for signal in result.signals}

    assert SignalType.PROJECT_ACTIVITY in types
    assert SignalType.CONTRACT_AWARD_ACTIVITY not in types
    assert SignalType.BID_ACTIVITY not in types


def test_bid_stage_never_becomes_contract_award_activity():
    evidence = [
        _evidence(
            "ev_bid",
            excerpt="Acme is the apparent low bidder.",
            evidence_type=EvidenceType.PROCUREMENT_NOTICE,
        )
    ]
    event = _event(EventType.APPARENT_LOW_BIDDER, ("ev_bid",))
    result = compute_company_signals([event], evidence, now=NOW)
    types = {signal.signal_type for signal in result.signals}

    assert SignalType.BID_ACTIVITY in types
    assert SignalType.CONTRACT_AWARD_ACTIVITY not in types


def test_same_project_is_counted_once_but_keeps_all_supporting_evidence():
    evidence = [
        _evidence("ev_city"),
        _evidence(
            "ev_news",
            source_url="https://news.example/story",
            publisher="Construction News",
            evidence_type=EvidenceType.NEWS_ARTICLE,
        ),
    ]
    events = [
        _event(EventType.CONTRACT_AWARDED, ("ev_city",)),
        _event(EventType.CONTRACT_AWARDED, ("ev_news",)),
    ]
    result = compute_company_signals(events, evidence, now=NOW)
    award = next(
        signal for signal in result.signals
        if signal.signal_type is SignalType.CONTRACT_AWARD_ACTIVITY
    )

    assert award.event_count == 1
    assert award.evidence_ids == ("ev_city", "ev_news")
    assert award.independent_source_count == 2


def test_later_more_authoritative_procurement_stage_marks_earlier_stage_stale():
    evidence = [
        _evidence(
            "ev_bid",
            published_at="2026-09-01",
            excerpt="Acme submitted a bid.",
            evidence_type=EvidenceType.PROCUREMENT_NOTICE,
        ),
        _evidence(
            "ev_award",
            published_at="2026-09-10",
            excerpt="Contract awarded to Acme.",
            evidence_type=EvidenceType.GOVERNMENT_AWARD,
        ),
    ]
    bid = _event(
        EventType.BID_SUBMITTED, ("ev_bid",), occurred_at="2026-09-01"
    )
    award = _event(
        EventType.CONTRACT_AWARDED, ("ev_award",), occurred_at="2026-09-10"
    )
    result = compute_company_signals([bid, award], evidence, now=NOW)

    assert bid.event_id in result.excluded_event_ids
    assert any(bid.event_id in item for item in result.contradictions)
    bid_signal = next(
        signal for signal in result.signals
        if signal.signal_type is SignalType.BID_ACTIVITY
    )
    assert bid_signal.event_count == 0
    assert bid_signal.strength is SignalStrength.LOW
    assert bid_signal.contradiction


def test_lower_authority_news_cannot_supersede_an_official_record():
    evidence = [
        _evidence(
            "ev_award",
            published_at="2026-09-01",
            evidence_type=EvidenceType.GOVERNMENT_AWARD,
        ),
        _evidence(
            "ev_news",
            source_url="https://news.example/story",
            publisher="Construction News",
            published_at="2026-09-10",
            excerpt="Acme signed the contract.",
            evidence_type=EvidenceType.NEWS_ARTICLE,
        ),
    ]
    award = _event(
        EventType.CONTRACT_AWARDED, ("ev_award",), occurred_at="2026-09-01"
    )
    signed = _event(
        EventType.CONTRACT_SIGNED, ("ev_news",), occurred_at="2026-09-10"
    )
    result = compute_company_signals([award, signed], evidence, now=NOW)

    assert award.event_id not in result.excluded_event_ids


def test_closed_job_observation_excludes_the_older_open_hiring_event():
    evidence = [
        _evidence(
            "ev_open",
            source_url="https://acme.com/careers/estimator",
            publisher="Acme",
            published_at="2026-08-01",
            excerpt="Senior Estimator position open.",
            evidence_type=EvidenceType.JOB_POSTING,
        ),
        _evidence(
            "ev_closed",
            source_url="https://acme.com/careers/estimator",
            publisher="Acme",
            published_at="2026-09-10",
            excerpt="Senior Estimator position filled.",
            evidence_type=EvidenceType.JOB_POSTING,
        ),
    ]
    open_event = _event(
        EventType.HIRING, ("ev_open",), project_key="senior-estimator",
        occurred_at="2026-08-01",
    )
    closed_event = _event(
        EventType.HIRING, ("ev_closed",), project_key="senior-estimator",
        occurred_at="2026-09-10",
    )
    result = compute_company_signals([open_event, closed_event], evidence, now=NOW)

    assert set(result.excluded_event_ids) == {open_event.event_id, closed_event.event_id}
    hiring = next(
        signal for signal in result.signals
        if signal.signal_type is SignalType.HIRING_ACTIVITY
    )
    assert hiring.event_count == 0
    assert hiring.strength is SignalStrength.LOW
    assert hiring.contradiction


def test_recent_official_direct_evidence_is_high_strength():
    evidence = [_evidence("ev_award")]
    event = _event(EventType.CONTRACT_AWARDED, ("ev_award",))
    result = compute_company_signals([event], evidence, now=NOW)
    award = next(
        signal for signal in result.signals
        if signal.signal_type is SignalType.CONTRACT_AWARD_ACTIVITY
    )

    assert award.score == 12
    assert award.strength is SignalStrength.HIGH
    assert award.recency is RecencyBucket.VERY_RECENT


def test_events_from_two_companies_are_refused():
    evidence = [
        _evidence("ev_a"),
        _evidence("ev_b", company_id="cmp_other"),
    ]
    events = [
        _event(EventType.HIRING, ("ev_a",), project_key="role-a"),
        _event(
            EventType.HIRING, ("ev_b",), company_id="cmp_other",
            project_key="role-b",
        ),
    ]

    import pytest

    with pytest.raises(ValueError, match="exactly one company"):
        compute_company_signals(events, evidence, now=NOW)
