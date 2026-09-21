"""Immutable, evidence-licensed outreach trigger records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any


class OutreachStrength(str, Enum):
    CONFIRMED = "confirmed"
    STRONG_INFERENCE = "strong_inference"
    WEAK_INFERENCE = "weak_inference"
    NONE = "none"


def new_trigger_id(company_id: str) -> str:
    return "out_" + hashlib.sha256(company_id.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class OutreachTriggerRecord:
    trigger_id: str
    company_id: str
    angle: str
    basis: str
    strength: OutreachStrength
    wording: str
    computed_at: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.trigger_id.strip() or not self.company_id.strip():
            raise ValueError("trigger_id and company_id are required")
        object.__setattr__(self, "strength", OutreachStrength(self.strength))
        object.__setattr__(self, "evidence_ids", tuple(dict.fromkeys(self.evidence_ids)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "company_id": self.company_id,
            "angle": self.angle,
            "basis": self.basis,
            "strength": self.strength.value,
            "wording": self.wording,
            "computed_at": self.computed_at,
            "evidence_ids": list(self.evidence_ids),
        }
