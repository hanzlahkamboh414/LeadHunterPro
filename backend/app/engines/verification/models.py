"""Structured data model for accuracy-first lead verification.

Phase 1 scope: the status enum, per-field evidence, and the lead-record
shape that later phases populate. Nothing in this module *performs*
verification — it only defines the structured container so provenance can
follow every record (CLAUDE.md §1, accuracy-first §10).

Rules this model encodes:
- ``city``/``state`` default to ``None`` (unknown) — missing location is
  NEVER filled from the user's query (accuracy-first Rule 1).
- ``verification_status``/``verification_confidence`` are set by the
  acceptance gate (Phase 2); ``ai``/``qualification`` by the AI
  intelligence stage (Phase 3). Until then a record is ``unknown`` with
  confidence ``0.0`` — no evidence = unknown, not true (Rule 8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.engines.verification.source_tiers import SourceTier


class VerificationStatus(str, Enum):
    """Verification outcome for a discovered company."""

    verified = "verified"
    partially_verified = "partially_verified"
    rejected = "rejected"
    unknown = "unknown"


@dataclass
class FieldEvidence:
    """One piece of evidence backing a single field (e.g. ``city``).

    ``fetched_at`` is auto-stamped when omitted so every piece of evidence
    carries a timestamp without callers having to supply one.
    """

    field: str
    value: Any
    source: str
    source_url: str = ""
    confidence: float = 0.0
    fetched_at: str = ""
    is_reliable: bool = False

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )


@dataclass
class LeadRecord:
    """Structured, evidence-backed record for one discovered company.

    Attributes:
        company_name/website/city/state/country: identity fields.
            city/state default to ``None`` (unknown) until verified.
        source/source_url: where this record came from.
        identity_evidence/industry_evidence/location_evidence: per-field
            evidence lists, populated as each stage collects provenance.
        business_type: contractor | manufacturer | supplier | association |
            unknown.
        verification_status: VerificationStatus.
        verification_confidence: 0-100, set by the acceptance gate.
        verification_reasons: human-readable reasons for the status.
        source_tier: Tier 1-4 (defaults to Tier 4 — the safest default).
        ai: raw AI assessment dict (Phase 3).
        qualification: final qualification metadata (Phase 3/4).
    """

    company_name: str = ""
    website: str = ""
    city: str | None = None
    state: str | None = None
    country: str = "USA"
    source: str = ""
    source_url: str = ""

    identity_evidence: list[FieldEvidence] = field(default_factory=list)
    industry_evidence: list[FieldEvidence] = field(default_factory=list)
    location_evidence: list[FieldEvidence] = field(default_factory=list)

    business_type: str = "unknown"

    verification_status: VerificationStatus = VerificationStatus.unknown
    verification_confidence: float = 0.0
    verification_reasons: list[str] = field(default_factory=list)
    source_tier: int = int(SourceTier.FIXTURE)

    ai: dict[str, Any] = field(default_factory=dict)
    qualification: dict[str, Any] = field(default_factory=dict)
