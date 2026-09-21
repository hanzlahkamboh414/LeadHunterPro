"""AI Call #2 plus the deterministic PAIN_GATE."""

from __future__ import annotations

import json
import hashlib
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.research.events import EventRecord
from app.research.models import CanonicalEvidence
from app.research.pain.models import (
    CandidatePainHypothesis,
    PainGateResult,
    PainHypothesisRecord,
    PainInferenceResult,
    PainType,
    PainVerdict,
    new_hypothesis_id,
)
from app.research.signals import (
    RecencyBucket,
    SignalRecord,
    SignalStrength,
    SignalType,
)
from app.research.taxonomy import CompanyMatch


VERIFIED_INFERENCE_CAP = 0.85
DIRECT_EVIDENCE_CAP = 0.95
HIGH_SIGNAL_CAP = 0.65
MEDIUM_SIGNAL_CAP = 0.50
WEAK_SIGNAL_CAP = 0.35

_RELEVANT_SIGNALS: dict[PainType, frozenset[SignalType]] = {
    PainType.ESTIMATING_CAPACITY: frozenset({
        SignalType.ESTIMATING_ACTIVITY,
        SignalType.HIRING_ACTIVITY,
        SignalType.PROJECT_ACTIVITY,
        SignalType.CONTRACT_AWARD_ACTIVITY,
        SignalType.BID_ACTIVITY,
    }),
    PainType.BID_DEADLINE_PRESSURE: frozenset({
        SignalType.BID_ACTIVITY,
        SignalType.ESTIMATING_ACTIVITY,
    }),
    PainType.BID_VOLUME: frozenset({SignalType.BID_ACTIVITY}),
    PainType.PRECONSTRUCTION_WORKLOAD: frozenset({
        SignalType.ESTIMATING_ACTIVITY,
        SignalType.PROJECT_ACTIVITY,
        SignalType.BID_ACTIVITY,
    }),
    PainType.HIRING_STAFFING_CAPACITY: frozenset({SignalType.HIRING_ACTIVITY}),
    PainType.EXPANSION_SCALING: frozenset({
        SignalType.GROWTH_ACTIVITY,
        SignalType.HIRING_ACTIVITY,
        SignalType.PROJECT_ACTIVITY,
    }),
    PainType.PROJECT_VOLUME: frozenset({
        SignalType.PROJECT_ACTIVITY,
        SignalType.CONTRACT_AWARD_ACTIVITY,
    }),
    PainType.SCOPE_TAKEOFF_WORKLOAD: frozenset({
        SignalType.ESTIMATING_ACTIVITY,
        SignalType.PROJECT_ACTIVITY,
        SignalType.BID_ACTIVITY,
    }),
}

_PRIMARY_SIGNAL: dict[PainType, SignalType] = {
    PainType.ESTIMATING_CAPACITY: SignalType.ESTIMATING_ACTIVITY,
    PainType.BID_DEADLINE_PRESSURE: SignalType.BID_ACTIVITY,
    PainType.BID_VOLUME: SignalType.BID_ACTIVITY,
    PainType.PRECONSTRUCTION_WORKLOAD: SignalType.ESTIMATING_ACTIVITY,
    PainType.HIRING_STAFFING_CAPACITY: SignalType.HIRING_ACTIVITY,
    PainType.EXPANSION_SCALING: SignalType.GROWTH_ACTIVITY,
    PainType.PROJECT_VOLUME: SignalType.PROJECT_ACTIVITY,
    PainType.SCOPE_TAKEOFF_WORKLOAD: SignalType.ESTIMATING_ACTIVITY,
}

_DIRECT_PHRASES: dict[PainType, tuple[str, ...]] = {
    PainType.ESTIMATING_CAPACITY: (
        "estimating capacity", "estimating team is overloaded",
        "need additional estimators", "need more estimators", "estimating backlog",
    ),
    PainType.BID_DEADLINE_PRESSURE: (
        "bid deadline pressure", "struggling to meet bid deadlines",
        "tight bid deadline",
    ),
    PainType.BID_VOLUME: ("bid volume", "increase in bids", "more bids than"),
    PainType.PRECONSTRUCTION_WORKLOAD: (
        "preconstruction workload", "preconstruction team is overloaded",
        "preconstruction backlog",
    ),
    PainType.HIRING_STAFFING_CAPACITY: (
        "staffing shortage", "understaffed", "hard to fill", "need additional staff",
    ),
    PainType.EXPANSION_SCALING: (
        "scaling challenge", "capacity to support growth", "growing faster than",
    ),
    PainType.PROJECT_VOLUME: ("project backlog", "record backlog", "project volume"),
    PainType.SCOPE_TAKEOFF_WORKLOAD: (
        "takeoff workload", "take-off workload", "scope review workload",
        "takeoff backlog",
    ),
}


class PainInferenceError(ValueError):
    """AI Call #2 response could not be interpreted safely."""


def pain_basis_hash(
    events: Sequence[EventRecord], signals: Sequence[SignalRecord]
) -> str:
    """Stable cache key for the factual snapshot consumed by AI Call #2."""
    event_rows = sorted(
        (item.to_row() for item in events), key=lambda row: row["event_id"]
    )
    signal_rows = []
    for item in signals:
        row = item.to_row()
        row.pop("computed_at", None)
        signal_rows.append(row)
    signal_rows.sort(key=lambda row: row["signal_id"])
    payload = json.dumps(
        {"events": event_rows, "signals": signal_rows},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prompt(events: Sequence[EventRecord], signals: Sequence[SignalRecord]) -> str:
    event_rows = [item.to_row() for item in events]
    signal_rows = [item.to_row() for item in signals]
    schema = {
        "candidate_signals": [{
            "signal_id": "one existing signal_id",
            "reasoning": "correlation reason",
        }],
        "candidate_pain_hypotheses": [{
            "pain_type": "one allowed pain type",
            "confidence": "number 0 to 1",
            "signal_ids": ["existing signal ids"],
            "evidence_ids": ["evidence ids already linked to those signals/events"],
            "reasoning": "bounded explanation",
        }],
    }
    return (
        "Correlate only the supplied verified company events and deterministic "
        "signals into candidate pain hypotheses. AI proposes; a deterministic "
        "gate decides. You MUST NOT search, browse, fetch URLs, or introduce "
        "outside facts. Raw web pages and canonical excerpts are not available. "
        "Three awards alone must never become estimating_capacity. Return JSON "
        f"only using pain types {[item.value for item in PainType]}. "
        f"Output shape: {json.dumps(schema)}. Events: "
        f"{json.dumps(event_rows, sort_keys=True)}. Signals: "
        f"{json.dumps(signal_rows, sort_keys=True)}"
    )


def _parse(raw: str) -> list[dict]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PainInferenceError("AI Call #2 did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise PainInferenceError("AI Call #2 JSON must be an object")
    proposals = payload.get("candidate_pain_hypotheses")
    if not isinstance(payload.get("candidate_signals"), list) or not isinstance(
        proposals, list
    ):
        raise PainInferenceError(
            "AI Call #2 JSON requires candidate_signals and "
            "candidate_pain_hypotheses lists"
        )
    if not all(isinstance(item, dict) for item in proposals):
        raise PainInferenceError("every pain proposal must be an object")
    return proposals


def _candidate(raw: dict) -> CandidatePainHypothesis:
    signal_ids = raw.get("signal_ids")
    evidence_ids = raw.get("evidence_ids")
    if not isinstance(signal_ids, list) or not isinstance(evidence_ids, list):
        raise TypeError("signal_ids and evidence_ids must be lists")
    raw_type = str(raw.get("pain_type") or "").strip().lower()
    try:
        pain_type = PainType(raw_type)
    except ValueError as exc:
        raise ValueError(f"unknown pain_type {raw_type!r}") from exc
    return CandidatePainHypothesis(
        pain_type=pain_type,
        confidence=raw.get("confidence"),
        signal_ids=tuple(str(value).strip() for value in signal_ids if str(value).strip()),
        evidence_ids=tuple(
            str(value).strip() for value in evidence_ids if str(value).strip()
        ),
        reasoning=str(raw.get("reasoning") or ""),
    )


def _source_key(item: CanonicalEvidence) -> str:
    if item.publisher.strip():
        return "publisher:" + item.publisher.strip().lower()
    return "host:" + (urlparse(item.source_url).hostname or "").lower()


def _direct_evidence(
    pain_type: PainType,
    evidence: Sequence[CanonicalEvidence],
) -> tuple[str, ...]:
    phrases = _DIRECT_PHRASES[pain_type]
    return tuple(
        item.evidence_id for item in evidence
        if any(phrase in f"{item.title} {item.excerpt}".lower() for phrase in phrases)
    )


def pain_gate(
    candidate: CandidatePainHypothesis,
    signals: Sequence[SignalRecord],
    evidence: Sequence[CanonicalEvidence],
) -> PainGateResult:
    """Evaluate every Phase 4 requirement and report every unmet condition."""
    signals_by_id = {item.signal_id: item for item in signals}
    evidence_by_id = {item.evidence_id: item for item in evidence}
    unknown_signals = [
        value for value in candidate.signal_ids if value not in signals_by_id
    ]
    unknown_evidence = [
        value for value in candidate.evidence_ids if value not in evidence_by_id
    ]
    known_signals = [
        signals_by_id[value] for value in candidate.signal_ids if value in signals_by_id
    ]
    known_evidence = [
        evidence_by_id[value]
        for value in candidate.evidence_ids if value in evidence_by_id
    ]

    blocked: list[str] = []
    if unknown_signals:
        blocked.append(f"unknown signal_id(s): {', '.join(unknown_signals)}")
    if unknown_evidence:
        blocked.append(f"unknown evidence_id(s): {', '.join(unknown_evidence)}")

    relevant = _RELEVANT_SIGNALS[candidate.pain_type]
    relevant_signals = [item for item in known_signals if item.signal_type in relevant]
    primary = _PRIMARY_SIGNAL[candidate.pain_type]
    has_primary = any(item.signal_type is primary for item in relevant_signals)
    if not has_primary:
        blocked.append(
            f"pain type {candidate.pain_type.value} requires relevant "
            f"{primary.value} support"
        )

    if not known_evidence or any(
        item.company_match is not CompanyMatch.CONFIRMED for item in known_evidence
    ):
        blocked.append("company identity is not confirmed for every supporting record")

    contradictions = [
        reason for item in known_signals for reason in item.contradiction
    ]
    if contradictions:
        blocked.append("support contains contradiction: " + "; ".join(contradictions))

    recent = any(
        item.recency in {RecencyBucket.VERY_RECENT, RecencyBucket.RECENT}
        for item in relevant_signals
    )
    if not recent:
        blocked.append("no relevant signal is within the 0-90 day recent threshold")

    direct_ids = _direct_evidence(candidate.pain_type, known_evidence)
    strong = [
        item for item in relevant_signals
        if item.strength is SignalStrength.HIGH and not item.contradiction
    ]
    source_keys = {
        _source_key(evidence_by_id[value])
        for signal in strong for value in signal.evidence_ids
        if value in evidence_by_id
    }
    independent_strong = len(strong) >= 2 and len(source_keys) >= 2
    if not direct_ids and not independent_strong:
        blocked.append(
            "requires direct pain evidence or at least two independent strong signals"
        )

    support_confidence = max((item.confidence for item in known_evidence), default=0.0)
    if direct_ids:
        licensed = min(DIRECT_EVIDENCE_CAP, support_confidence)
    elif independent_strong:
        licensed = min(VERIFIED_INFERENCE_CAP, support_confidence)
    elif any(item.strength is SignalStrength.HIGH for item in relevant_signals):
        licensed = min(HIGH_SIGNAL_CAP, support_confidence)
    elif any(item.strength is SignalStrength.MEDIUM for item in relevant_signals):
        licensed = min(MEDIUM_SIGNAL_CAP, support_confidence)
    else:
        licensed = min(WEAK_SIGNAL_CAP, support_confidence)
    confidence_ok = candidate.confidence <= licensed
    if not confidence_ok:
        blocked.append(
            f"candidate confidence {candidate.confidence:.2f} exceeds "
            f"evidence-licensed {licensed:.2f}"
        )

    unsafe = bool(unknown_signals or unknown_evidence or contradictions) or (
        bool(known_evidence)
        and any(item.company_match is not CompanyMatch.CONFIRMED for item in known_evidence)
    )
    verified_basis = bool(direct_ids or independent_strong)
    if unsafe:
        verdict = PainVerdict.BLOCKED
    elif has_primary and recent and verified_basis and confidence_ok:
        verdict = PainVerdict.VERIFIED
    elif has_primary and recent and (
        verified_basis or any(
            item.strength in {SignalStrength.HIGH, SignalStrength.MEDIUM}
            for item in relevant_signals
        )
    ):
        verdict = PainVerdict.LIKELY
    else:
        verdict = PainVerdict.UNKNOWN
    return PainGateResult(
        verdict=verdict,
        blocked_by=tuple(blocked),
        licensed_confidence=licensed,
        direct_evidence_ids=direct_ids,
    )


def infer_company_pain(
    events: Sequence[EventRecord],
    signals: Sequence[SignalRecord],
    evidence: Sequence[CanonicalEvidence],
    *,
    ai_ask: Callable[[str], str],
) -> PainInferenceResult:
    """Make one correlation call, then deterministically gate every proposal."""
    event_snapshot = tuple(events)
    signal_snapshot = tuple(signals)
    evidence_snapshot = tuple(evidence)
    if not event_snapshot or not signal_snapshot:
        return PainInferenceResult()
    company_ids = {
        item.company_id for item in event_snapshot + signal_snapshot + evidence_snapshot
    }
    if len(company_ids) != 1:
        raise ValueError("pain inference accepts data for exactly one company")
    company_id = next(iter(company_ids))
    proposals = _parse(ai_ask(_prompt(event_snapshot, signal_snapshot)))
    accepted: list[PainHypothesisRecord] = []
    rejected: list[str] = []
    seen_types: set[PainType] = set()
    computed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for index, raw in enumerate(proposals):
        try:
            candidate = _candidate(raw)
            if candidate.pain_type in seen_types:
                raise ValueError(f"duplicate pain_type {candidate.pain_type.value}")
            seen_types.add(candidate.pain_type)
            gate = pain_gate(candidate, signal_snapshot, evidence_snapshot)
            accepted.append(PainHypothesisRecord(
                hypothesis_id=new_hypothesis_id(company_id, candidate.pain_type),
                company_id=company_id,
                pain_type=candidate.pain_type,
                confidence=candidate.confidence,
                licensed_confidence=gate.licensed_confidence,
                verdict=gate.verdict,
                blocked_by=gate.blocked_by,
                computed_at=computed_at,
                reasoning=candidate.reasoning,
                evidence_ids=candidate.evidence_ids,
                signal_ids=candidate.signal_ids,
                direct_evidence_ids=gate.direct_evidence_ids,
            ))
        except (TypeError, ValueError) as exc:
            rejected.append(f"proposal {index}: {exc}")
    return PainInferenceResult(tuple(accepted), tuple(rejected))
