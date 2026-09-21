"""AI Call #1 and deterministic event normalization (layers L6-L7)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from app.research.events.models import (
    EventExtractionResult,
    EventRecord,
    new_event_id,
)
from app.research.models import CanonicalEvidence
from app.research.taxonomy import EventType, EvidenceType


class EventExtractionError(ValueError):
    """The extractor response could not be interpreted safely."""


_PROCUREMENT_PROOF: dict[EventType, tuple[str, ...]] = {
    EventType.BID_SUBMITTED: (
        "bid submitted", "submitted a bid", "submitted its bid", "proposal submitted",
    ),
    EventType.APPARENT_LOW_BIDDER: (
        "apparent low bidder", "lowest responsive bidder", "apparent lowest bidder",
    ),
    EventType.RECOMMENDED_FOR_AWARD: (
        "recommended for award", "recommend award", "intent to award",
        "notice of intent to award",
    ),
    EventType.CONTRACT_AWARDED: (
        "contract awarded", "awarded the contract", "award to ",
        "has been awarded", "was awarded",
    ),
    EventType.CONTRACT_SIGNED: (
        "contract signed", "signed the contract", "executed contract",
        "contract executed",
    ),
}


def _prompt(evidence: Sequence[CanonicalEvidence]) -> str:
    allowed = [
        value.value for value in EventType if value is not EventType.NEEDS_RESOLUTION
    ]
    records = [
        {
            "evidence_id": item.evidence_id,
            "source_url": item.source_url,
            "source_type": item.source_type,
            "publisher": item.publisher,
            "title": item.title,
            "excerpt": item.excerpt,
            "published_at": item.published_at,
            "company_match": item.company_match.value,
            "evidence_type": str(item.evidence_type),
            "project_key": item.project_key,
            "confidence": item.confidence,
        }
        for item in evidence
    ]
    schema = {
        "events": [{
            "event_type": "one allowed value",
            "project_key": "existing project_key or empty",
            "occurred_at": "date stated by evidence or empty",
            "confidence": "number 0 to 1",
            "evidence_ids": ["supporting evidence ids"],
        }]
    }
    return (
        "Extract factual company events from the supplied canonical evidence. "
        "Return JSON only. Do not infer pain, workload, intent, or facts absent "
        "from the quoted evidence. Use only these event_type values: "
        f"{json.dumps(allowed)}. Output shape: {json.dumps(schema)}. "
        f"Canonical evidence: {json.dumps(records, ensure_ascii=False, sort_keys=True)}"
    )


def _parse(raw: str) -> list[dict[str, Any]]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EventExtractionError("AI Call #1 did not return valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise EventExtractionError("AI Call #1 JSON must contain an events list")
    if not all(isinstance(item, dict) for item in payload["events"]):
        raise EventExtractionError("every event proposal must be an object")
    return payload["events"]


def _support_text(items: Sequence[CanonicalEvidence]) -> str:
    return " ".join(f"{item.title} {item.excerpt}".lower() for item in items)


def _identity_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _matches_company(item: CanonicalEvidence, company_name: str) -> bool:
    if item.company_match.value == "confirmed":
        return True
    if item.source_type.strip().lower() == "company_site":
        return True
    expected = _identity_text(company_name)
    if not expected:
        return False
    observed = _identity_text(" ".join((item.publisher, item.title, item.excerpt)))
    return expected in observed


def _normalize(
    proposal: dict[str, Any],
    evidence_by_id: dict[str, CanonicalEvidence],
    company_id: str,
) -> EventRecord:
    raw_type = str(proposal.get("event_type") or "").strip().lower()
    try:
        event_type = EventType(raw_type)
    except ValueError as exc:
        raise ValueError(f"unknown event_type {raw_type!r}") from exc
    if event_type is EventType.NEEDS_RESOLUTION:
        raise ValueError("needs_resolution is not a factual event")

    raw_ids = proposal.get("evidence_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("event proposal requires evidence_ids")
    evidence_ids = tuple(dict.fromkeys(str(value).strip() for value in raw_ids))
    unknown = [value for value in evidence_ids if value not in evidence_by_id]
    if unknown:
        raise ValueError(f"unknown evidence_id(s): {', '.join(unknown)}")
    support = [evidence_by_id[value] for value in evidence_ids]

    if event_type is not EventType.PERMIT_ISSUED and all(
        str(item.evidence_type) == EvidenceType.PERMIT_RECORD.value for item in support
    ):
        raise ValueError(
            f"permit evidence cannot establish {event_type.value}; a permit is activity"
        )
    definitive_award = event_type is EventType.CONTRACT_AWARDED and any(
        str(item.evidence_type) == EvidenceType.GOVERNMENT_AWARD.value
        and item.source_type.strip().lower() == "usaspending"
        for item in support
    )
    if (
        event_type in _PROCUREMENT_PROOF
        and not definitive_award
        and not any(
            phrase in _support_text(support)
            for phrase in _PROCUREMENT_PROOF[event_type]
        )
    ):
        raise ValueError(
            f"source text does not establish procurement stage {event_type.value}"
        )

    project = str(proposal.get("project_key") or "").strip()
    known_projects = {item.project_key for item in support if item.project_key}
    if project and known_projects and project not in known_projects:
        raise ValueError(f"project_key {project!r} is not present in supporting evidence")
    if not project and len(known_projects) == 1:
        project = next(iter(known_projects))

    occurred_at = str(proposal.get("occurred_at") or "").strip()
    known_dates = {item.published_at.strip() for item in support if item.published_at.strip()}
    if occurred_at and known_dates:
        literal_support = any(
            occurred_at.lower() in f"{item.title} {item.excerpt}".lower()
            for item in support
        )
        if occurred_at not in known_dates and not literal_support:
            raise ValueError(
                f"occurred_at {occurred_at!r} is not grounded in supporting evidence"
            )
    if not occurred_at:
        if len(known_dates) == 1:
            occurred_at = next(iter(known_dates))
    confidence = proposal.get("confidence")
    if not isinstance(confidence, (int, float)):
        raise TypeError("event confidence must be numeric")

    return EventRecord(
        event_id=new_event_id(company_id, event_type, project, occurred_at),
        company_id=company_id,
        event_type=event_type,
        project_key=project,
        occurred_at=occurred_at,
        confidence=float(confidence),
        evidence_ids=evidence_ids,
    )


def _merge(events: Sequence[EventRecord]) -> tuple[EventRecord, ...]:
    merged: dict[tuple[str, str, str], EventRecord] = {}
    for event in events:
        key = (event.event_type.value, event.project_key, event.occurred_at)
        previous = merged.get(key)
        if previous is None:
            merged[key] = event
            continue
        ids = tuple(dict.fromkeys(previous.evidence_ids + event.evidence_ids))
        merged[key] = EventRecord(
            event_id=previous.event_id,
            company_id=previous.company_id,
            event_type=previous.event_type,
            project_key=previous.project_key,
            occurred_at=previous.occurred_at,
            confidence=max(previous.confidence, event.confidence),
            evidence_ids=ids,
        )
    return tuple(merged.values())


def extract_company_events(
    evidence: Sequence[CanonicalEvidence],
    *,
    ai_ask: Callable[[str], str],
    company_name: str = "",
) -> EventExtractionResult:
    """Make one AI extraction call, then deterministically accept/reject proposals."""
    snapshot = tuple(evidence)
    if not snapshot:
        return EventExtractionResult()
    company_ids = {item.company_id for item in snapshot}
    if len(company_ids) != 1:
        raise ValueError("event extraction accepts evidence for exactly one company")
    company_id = next(iter(company_ids))
    eligible = tuple(item for item in snapshot if _matches_company(item, company_name))
    excluded = len(snapshot) - len(eligible)
    identity_rejections = (
        (f"{excluded} evidence row(s) excluded: company identity was not confirmed",)
        if excluded else ()
    )
    if not eligible:
        return EventExtractionResult(rejections=identity_rejections)
    evidence_by_id = {item.evidence_id: item for item in eligible}
    proposals = _parse(ai_ask(_prompt(eligible)))
    accepted: list[EventRecord] = []
    rejected: list[str] = []
    for index, proposal in enumerate(proposals):
        try:
            accepted.append(_normalize(proposal, evidence_by_id, company_id))
        except (TypeError, ValueError) as exc:
            rejected.append(f"proposal {index}: {exc}")
    return EventExtractionResult(
        events=_merge(accepted), rejections=identity_rejections + tuple(rejected)
    )
