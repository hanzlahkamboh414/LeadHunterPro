"""Immutable event records produced from canonical evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.research.taxonomy import EventType


def new_event_id(
    company_id: str,
    event_type: EventType,
    project_key: str,
    occurred_at: str,
) -> str:
    """Return a stable id for one company event."""
    raw = "|".join((company_id, event_type.value, project_key, occurred_at))
    return "evt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class EventRecord:
    """One factual occurrence linked to every supporting evidence row."""

    event_id: str
    company_id: str
    event_type: EventType
    project_key: str
    occurred_at: str
    confidence: float
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.company_id.strip():
            raise ValueError("event_id and company_id are required")
        event_type = (
            self.event_type
            if isinstance(self.event_type, EventType)
            else EventType(str(self.event_type).strip().lower())
        )
        if event_type is EventType.NEEDS_RESOLUTION:
            raise ValueError("needs_resolution is not a factual event")
        if not isinstance(self.confidence, (int, float)):
            raise TypeError("event confidence must be numeric")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("event confidence must be within 0.0-1.0")
        evidence_ids = tuple(dict.fromkeys(
            str(value).strip() for value in self.evidence_ids if str(value).strip()
        ))
        if not evidence_ids:
            raise ValueError("an event requires at least one evidence_id")
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "project_key", (self.project_key or "").strip())
        object.__setattr__(self, "occurred_at", (self.occurred_at or "").strip())
        object.__setattr__(self, "evidence_ids", evidence_ids)

    def to_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "company_id": self.company_id,
            "event_type": self.event_type.value,
            "project_key": self.project_key,
            "occurred_at": self.occurred_at,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class EventExtractionResult:
    """Accepted events plus explicit reasons for every rejected proposal."""

    events: tuple[EventRecord, ...] = ()
    rejections: tuple[str, ...] = ()
