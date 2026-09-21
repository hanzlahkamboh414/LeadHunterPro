"""Immutable records produced by the deterministic Phase 3 signal engine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any


class RecencyBucket(str, Enum):
    VERY_RECENT = "very_recent"
    RECENT = "recent"
    MODERATE = "moderate"
    HISTORICAL = "historical"
    BACKGROUND = "background"


class SignalStrength(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SignalType(str, Enum):
    """Observable activity families; none of these is a pain hypothesis."""

    RECENT_ACTIVITY = "recent_activity"
    CONTRACT_AWARD_ACTIVITY = "contract_award_activity"
    BID_ACTIVITY = "bid_activity"
    PROJECT_ACTIVITY = "project_activity"
    HIRING_ACTIVITY = "hiring_activity"
    ESTIMATING_ACTIVITY = "estimating_activity"
    GROWTH_ACTIVITY = "growth_activity"


def new_signal_id(company_id: str, signal_type: SignalType) -> str:
    raw = f"{company_id}|{signal_type.value}"
    return "sig_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    company_id: str
    signal_type: SignalType
    strength: SignalStrength
    score: int
    computed_at: str
    contradiction: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    event_count: int
    independent_source_count: int
    recency: RecencyBucket

    def __post_init__(self) -> None:
        if not self.signal_id.strip() or not self.company_id.strip():
            raise ValueError("signal_id and company_id are required")
        object.__setattr__(self, "signal_type", SignalType(self.signal_type))
        object.__setattr__(self, "strength", SignalStrength(self.strength))
        object.__setattr__(self, "recency", RecencyBucket(self.recency))
        if not isinstance(self.score, int) or self.score < 0:
            raise ValueError("signal score must be a non-negative integer")
        if self.event_count < 0 or self.independent_source_count < 0:
            raise ValueError("signal counts must be non-negative")
        object.__setattr__(self, "contradiction", tuple(dict.fromkeys(self.contradiction)))
        object.__setattr__(self, "evidence_ids", tuple(dict.fromkeys(self.evidence_ids)))
        object.__setattr__(self, "event_ids", tuple(dict.fromkeys(self.event_ids)))

    def to_row(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "company_id": self.company_id,
            "signal_type": self.signal_type.value,
            "strength": self.strength.value,
            "score": self.score,
            "computed_at": self.computed_at,
            "contradiction": list(self.contradiction),
            "evidence_ids": list(self.evidence_ids),
            "event_ids": list(self.event_ids),
            "event_count": self.event_count,
            "independent_source_count": self.independent_source_count,
            "recency": self.recency.value,
        }


@dataclass(frozen=True)
class SignalComputationResult:
    signals: tuple[SignalRecord, ...] = ()
    contradictions: tuple[str, ...] = ()
    excluded_event_ids: tuple[str, ...] = ()
