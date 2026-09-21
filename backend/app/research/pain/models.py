"""Immutable Phase 4 pain candidates, gate verdicts and stored hypotheses."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PainType(str, Enum):
    ESTIMATING_CAPACITY = "estimating_capacity"
    BID_DEADLINE_PRESSURE = "bid_deadline_pressure"
    BID_VOLUME = "bid_volume"
    PRECONSTRUCTION_WORKLOAD = "preconstruction_workload"
    HIRING_STAFFING_CAPACITY = "hiring_staffing_capacity"
    EXPANSION_SCALING = "expansion_scaling"
    PROJECT_VOLUME = "project_volume"
    SCOPE_TAKEOFF_WORKLOAD = "scope_takeoff_workload"


class PainVerdict(str, Enum):
    VERIFIED = "VERIFIED"
    LIKELY = "LIKELY"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"


def new_hypothesis_id(company_id: str, pain_type: PainType) -> str:
    raw = f"{company_id}|{pain_type.value}"
    return "pain_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class CandidatePainHypothesis:
    pain_type: PainType
    confidence: float
    signal_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    reasoning: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "pain_type", PainType(self.pain_type))
        if not isinstance(self.confidence, (int, float)):
            raise TypeError("pain confidence must be numeric")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("pain confidence must be within 0.0-1.0")
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "signal_ids", tuple(dict.fromkeys(self.signal_ids)))
        object.__setattr__(self, "evidence_ids", tuple(dict.fromkeys(self.evidence_ids)))
        object.__setattr__(self, "reasoning", (self.reasoning or "").strip())


@dataclass(frozen=True)
class PainGateResult:
    verdict: PainVerdict
    blocked_by: tuple[str, ...]
    licensed_confidence: float
    direct_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PainHypothesisRecord:
    hypothesis_id: str
    company_id: str
    pain_type: PainType
    confidence: float
    licensed_confidence: float
    verdict: PainVerdict
    blocked_by: tuple[str, ...]
    computed_at: str
    reasoning: str
    evidence_ids: tuple[str, ...]
    signal_ids: tuple[str, ...]
    direct_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.hypothesis_id.strip() or not self.company_id.strip():
            raise ValueError("hypothesis_id and company_id are required")
        object.__setattr__(self, "pain_type", PainType(self.pain_type))
        object.__setattr__(self, "verdict", PainVerdict(self.verdict))
        for name, value in (
            ("confidence", self.confidence),
            ("licensed_confidence", self.licensed_confidence),
        ):
            if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be within 0.0-1.0")
            object.__setattr__(self, name, float(value))
        object.__setattr__(self, "blocked_by", tuple(dict.fromkeys(self.blocked_by)))
        object.__setattr__(self, "evidence_ids", tuple(dict.fromkeys(self.evidence_ids)))
        object.__setattr__(self, "signal_ids", tuple(dict.fromkeys(self.signal_ids)))
        object.__setattr__(
            self, "direct_evidence_ids", tuple(dict.fromkeys(self.direct_evidence_ids))
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "company_id": self.company_id,
            "pain_type": self.pain_type.value,
            "confidence": self.confidence,
            "licensed_confidence": self.licensed_confidence,
            "verdict": self.verdict.value,
            "blocked_by": list(self.blocked_by),
            "computed_at": self.computed_at,
            "reasoning": self.reasoning,
            "evidence_ids": list(self.evidence_ids),
            "signal_ids": list(self.signal_ids),
            "direct_evidence_ids": list(self.direct_evidence_ids),
        }


@dataclass(frozen=True)
class PainInferenceResult:
    hypotheses: tuple[PainHypothesisRecord, ...] = ()
    rejections: tuple[str, ...] = ()
