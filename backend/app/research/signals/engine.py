"""Deterministic event-to-signal scoring, recency and contradiction rules."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from app.research.events import EventRecord
from app.research.models import CanonicalEvidence
from app.research.signals.models import (
    RecencyBucket,
    SignalComputationResult,
    SignalRecord,
    SignalStrength,
    SignalType,
    new_signal_id,
)
from app.research.taxonomy import (
    BID_AWARD_CHAIN,
    CONTRACT_AWARD_EVENTS,
    EventType,
    EvidenceType,
    chain_rank,
)


DIRECT_EVIDENCE_WEIGHT = 5
OFFICIAL_SOURCE_WEIGHT = 4
INDEPENDENT_SOURCE_WEIGHT = 3
RECENT_WEIGHT = 3
REPEATED_SIGNAL_WEIGHT = 3
WEAK_INFERENCE_WEIGHT = 1
CONTRADICTION_WEIGHT = -4
STALE_WEIGHT = -2

HIGH_SCORE_MIN = 12
MEDIUM_SCORE_MIN = 8

_BID_EVENTS = frozenset(BID_AWARD_CHAIN[:3])
_PROJECT_EVENTS = frozenset({
    EventType.PROJECT_ANNOUNCED,
    EventType.PROJECT_COMPLETED,
    EventType.PERMIT_ISSUED,
    EventType.PAYMENT_RECORDED,
    *CONTRACT_AWARD_EVENTS,
})
_GROWTH_EVENTS = frozenset({EventType.NEW_OFFICE, EventType.MARKET_EXPANSION})
_OFFICIAL_EVIDENCE = frozenset({
    EvidenceType.GOVERNMENT_AWARD.value,
    EvidenceType.PROCUREMENT_NOTICE.value,
    EvidenceType.PERMIT_RECORD.value,
    EvidenceType.BUSINESS_FILING.value,
    EvidenceType.COMPANY_PAGE.value,
    EvidenceType.JOB_POSTING.value,
})
_ESTIMATING_TERMS = re.compile(
    r"\b(estimat(?:e|es|ing|or|ors)|preconstruction|takeoff|take-off)\b",
    re.IGNORECASE,
)
_CLOSED_JOB_TERMS = re.compile(
    r"\b(position|role|opening|vacancy|job)\b.{0,40}\b(filled|closed)\b|"
    r"\b(filled|closed)\b.{0,40}\b(position|role|opening|vacancy|job)\b",
    re.IGNORECASE,
)


def _as_datetime(value: str) -> datetime | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(cleaned[:10]), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def recency_bucket(
    occurred_at: str,
    *,
    now: datetime | None = None,
) -> RecencyBucket:
    """Map an event date onto the frozen 0/30/90/180/365-day boundaries."""
    observed = _as_datetime(occurred_at)
    if observed is None:
        return RecencyBucket.BACKGROUND
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = max(0, (current.date() - observed.date()).days)
    if age <= 30:
        return RecencyBucket.VERY_RECENT
    if age <= 90:
        return RecencyBucket.RECENT
    if age <= 180:
        return RecencyBucket.MODERATE
    if age <= 365:
        return RecencyBucket.HISTORICAL
    return RecencyBucket.BACKGROUND


def _authority(item: CanonicalEvidence) -> int:
    kind = str(item.evidence_type)
    if kind in {
        EvidenceType.GOVERNMENT_AWARD.value,
        EvidenceType.PROCUREMENT_NOTICE.value,
        EvidenceType.PERMIT_RECORD.value,
        EvidenceType.BUSINESS_FILING.value,
    }:
        return 4
    if kind in {EvidenceType.COMPANY_PAGE.value, EvidenceType.JOB_POSTING.value}:
        return 3
    if kind == EvidenceType.NEWS_ARTICLE.value:
        return 2
    return 1


def _event_authority(
    event: EventRecord,
    evidence_by_id: dict[str, CanonicalEvidence],
) -> int:
    return max(
        (_authority(evidence_by_id[value]) for value in event.evidence_ids),
        default=0,
    )


def _event_date(event: EventRecord) -> datetime | None:
    return _as_datetime(event.occurred_at)


def _support_text(
    event: EventRecord,
    evidence_by_id: dict[str, CanonicalEvidence],
) -> str:
    return " ".join(
        f"{evidence_by_id[value].title} {evidence_by_id[value].excerpt}"
        for value in event.evidence_ids
    )


def _contradictions(
    events: Sequence[EventRecord],
    evidence_by_id: dict[str, CanonicalEvidence],
) -> tuple[set[str], tuple[str, ...]]:
    excluded: set[str] = set()
    reasons: list[str] = []
    by_project: dict[str, list[EventRecord]] = defaultdict(list)
    for event in events:
        if event.project_key:
            by_project[event.project_key].append(event)

    for project, candidates in by_project.items():
        procurement = [item for item in candidates if chain_rank(item.event_type) is not None]
        for older in procurement:
            older_date = _event_date(older)
            older_rank = chain_rank(older.event_type)
            if older_date is None or older_rank is None:
                continue
            for later in procurement:
                later_date = _event_date(later)
                later_rank = chain_rank(later.event_type)
                if (
                    later_date is not None
                    and later_rank is not None
                    and later_date > older_date
                    and later_rank > older_rank
                    and _event_authority(later, evidence_by_id)
                    >= _event_authority(older, evidence_by_id)
                ):
                    excluded.add(older.event_id)
                    reasons.append(
                        f"{older.event_id} stale: {later.event_id} records later "
                        f"{later.event_type.value} for project {project}"
                    )
                    break

        hiring = [item for item in candidates if item.event_type is EventType.HIRING]
        closures = [
            item for item in hiring
            if _CLOSED_JOB_TERMS.search(_support_text(item, evidence_by_id))
        ]
        for closure in closures:
            excluded.add(closure.event_id)
            closure_date = _event_date(closure)
            for opening in hiring:
                opening_date = _event_date(opening)
                if (
                    opening.event_id != closure.event_id
                    and closure_date is not None
                    and opening_date is not None
                    and closure_date > opening_date
                    and _event_authority(closure, evidence_by_id)
                    >= _event_authority(opening, evidence_by_id)
                ):
                    excluded.add(opening.event_id)
                    reasons.append(
                        f"{opening.event_id} stale: {closure.event_id} records "
                        f"the hiring position as filled/closed"
                    )
    return excluded, tuple(dict.fromkeys(reasons))


def _families(
    event: EventRecord,
    evidence_by_id: dict[str, CanonicalEvidence],
    *,
    now: datetime,
) -> set[SignalType]:
    families: set[SignalType] = set()
    if recency_bucket(event.occurred_at, now=now) in {
        RecencyBucket.VERY_RECENT, RecencyBucket.RECENT,
    }:
        families.add(SignalType.RECENT_ACTIVITY)
    if event.event_type in CONTRACT_AWARD_EVENTS:
        families.add(SignalType.CONTRACT_AWARD_ACTIVITY)
    if event.event_type in _BID_EVENTS:
        families.add(SignalType.BID_ACTIVITY)
    if event.event_type in _PROJECT_EVENTS:
        families.add(SignalType.PROJECT_ACTIVITY)
    if event.event_type is EventType.HIRING:
        families.add(SignalType.HIRING_ACTIVITY)
        if _ESTIMATING_TERMS.search(_support_text(event, evidence_by_id)):
            families.add(SignalType.ESTIMATING_ACTIVITY)
    if event.event_type in _GROWTH_EVENTS:
        families.add(SignalType.GROWTH_ACTIVITY)
    return families


def _source_key(item: CanonicalEvidence) -> str:
    if item.publisher.strip():
        return "publisher:" + item.publisher.strip().lower()
    return "host:" + (urlparse(item.source_url).hostname or "").lower()


def _dedup_count(events: Sequence[EventRecord]) -> int:
    keys = {
        ("project", item.project_key) if item.project_key
        else ("event", item.event_id)
        for item in events
    }
    return len(keys)


def _strength(score: int) -> SignalStrength:
    if score >= HIGH_SCORE_MIN:
        return SignalStrength.HIGH
    if score >= MEDIUM_SCORE_MIN:
        return SignalStrength.MEDIUM
    return SignalStrength.LOW


def compute_company_signals(
    events: Sequence[EventRecord],
    evidence: Sequence[CanonicalEvidence],
    *,
    now: datetime | None = None,
) -> SignalComputationResult:
    """Convert one company's verified events into deterministic observations."""
    event_snapshot = tuple(events)
    evidence_snapshot = tuple(evidence)
    company_ids = {item.company_id for item in event_snapshot + evidence_snapshot}
    if len(company_ids) > 1:
        raise ValueError("signal computation accepts events/evidence for exactly one company")
    if not event_snapshot:
        return SignalComputationResult()
    company_id = event_snapshot[0].company_id
    evidence_by_id = {item.evidence_id: item for item in evidence_snapshot}
    missing = sorted({
        value for event in event_snapshot for value in event.evidence_ids
        if value not in evidence_by_id
    })
    if missing:
        raise ValueError(f"unknown evidence_id(s): {', '.join(missing)}")
    if any(event.company_id != company_id for event in event_snapshot):
        raise ValueError("signal computation accepts events for exactly one company")

    current = now or datetime.now(timezone.utc)
    excluded, contradictions = _contradictions(event_snapshot, evidence_by_id)
    raw_by_family: dict[SignalType, list[EventRecord]] = defaultdict(list)
    active_by_family: dict[SignalType, list[EventRecord]] = defaultdict(list)
    for event in event_snapshot:
        for family in _families(event, evidence_by_id, now=current):
            raw_by_family[family].append(event)
            if event.event_id not in excluded:
                active_by_family[family].append(event)

    records: list[SignalRecord] = []
    computed_at = current.astimezone(timezone.utc).isoformat(timespec="seconds")
    for family in sorted(raw_by_family, key=lambda value: value.value):
        active = active_by_family.get(family, [])
        raw = raw_by_family[family]
        relevant_contradictions = tuple(
            reason for reason in contradictions
            if any(item.event_id in reason for item in raw)
        )
        evidence_ids = tuple(dict.fromkeys(
            value for event in active for value in event.evidence_ids
        ))
        if not evidence_ids and relevant_contradictions:
            evidence_ids = tuple(dict.fromkeys(
                value for event in raw for value in event.evidence_ids
            ))
        support = [evidence_by_id[value] for value in evidence_ids]
        independent = len({_source_key(item) for item in support if _source_key(item)})
        event_count = _dedup_count(active)
        buckets = [recency_bucket(item.occurred_at, now=current) for item in active]
        recency = min(
            buckets,
            key=lambda value: list(RecencyBucket).index(value),
            default=RecencyBucket.BACKGROUND,
        )
        score = 0
        if event_count:
            score += DIRECT_EVIDENCE_WEIGHT
        if any(str(item.evidence_type) in _OFFICIAL_EVIDENCE for item in support):
            score += OFFICIAL_SOURCE_WEIGHT
        if independent >= 2:
            score += INDEPENDENT_SOURCE_WEIGHT
        if recency in {RecencyBucket.VERY_RECENT, RecencyBucket.RECENT}:
            score += RECENT_WEIGHT
        if event_count >= 2:
            score += REPEATED_SIGNAL_WEIGHT
        if relevant_contradictions:
            score += CONTRADICTION_WEIGHT
        if active and all(
            bucket in {RecencyBucket.HISTORICAL, RecencyBucket.BACKGROUND}
            for bucket in buckets
        ):
            score += STALE_WEIGHT
        score = max(0, score)
        records.append(SignalRecord(
            signal_id=new_signal_id(company_id, family),
            company_id=company_id,
            signal_type=family,
            strength=_strength(score),
            score=score,
            computed_at=computed_at,
            contradiction=relevant_contradictions,
            evidence_ids=evidence_ids,
            event_ids=tuple(item.event_id for item in active),
            event_count=event_count,
            independent_source_count=independent,
            recency=recency,
        ))
    return SignalComputationResult(
        signals=tuple(records),
        contradictions=contradictions,
        excluded_event_ids=tuple(sorted(excluded)),
    )
