"""Person Attribution Research — data models (V1, additive).

This package is an additive side-band that attributes an otherwise
unattributed company email to a person. It NEVER touches the frozen parser
(A/B/C/D1), the gate, the pipeline, or ``lead_models.py`` — those files are
imported read-only where a helper is reused.

The two state models are deliberately separate (see ``docs/person_research_v1_spec.md``):
- :class:`ResearchStatus` is *operational* — what the queue is doing with a job.
- :class:`AttributionVerdict` is *semantic* — what we concluded about the person.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResearchStatus(str, Enum):
    """Operational state of one email-research job (drives resume/retry)."""

    pending = "pending"
    researching = "researching"
    completed = "completed"
    failed = "failed"


class AttributionVerdict(str, Enum):
    """Semantic conclusion about the person for one email."""

    unattributed = "unattributed"  # no eligible email, or nothing usable found
    candidates_found = "candidates_found"  # >=1 candidate, none confidently bound
    attributed = "attributed"  # exactly one person bound
    unresolved = "unresolved"  # contradictory / indeterminate — decision not to decide


class LocalPartMatchLevel(str, Enum):
    """Deterministic level of local-part <-> candidate-name match."""

    exact = "exact"
    first_last = "first_last"
    initial_last = "initial_last"
    first = "first"
    reject = "reject"  # initials / nickname / partial — never supports binding


@dataclass
class ResearchEvidence:
    """One traceable attribution signal.

    ``source_url`` is mandatory — a signal that cannot be traced to a URL is
    not evidence (hard rule #4). Only a short snippet is stored, never the full
    page body, so large page content is never duplicated across leads.
    """

    source_url: str
    source_type: str  # company_site | indexed_web | directory
    authority: str  # authoritative | supporting
    evidence_kind: str  # email_present | name_co_occurrence | local_part_match | role | corroboration | contradiction
    retrieved_at: str = ""
    snippet: str = ""
    canonical_url: str = ""

    def __post_init__(self) -> None:
        if not self.canonical_url:
            self.canonical_url = self.source_url

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "source_type": self.source_type,
            "authority": self.authority,
            "evidence_kind": self.evidence_kind,
            "retrieved_at": self.retrieved_at,
            "snippet": self.snippet,
            "canonical_url": self.canonical_url,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ResearchEvidence":
        return ResearchEvidence(
            source_url=d.get("source_url", ""),
            source_type=d.get("source_type", ""),
            authority=d.get("authority", "supporting"),
            evidence_kind=d.get("evidence_kind", ""),
            retrieved_at=d.get("retrieved_at", ""),
            snippet=d.get("snippet", ""),
            canonical_url=d.get("canonical_url", ""),
        )


@dataclass
class PersonCandidate:
    """One candidate person that MIGHT own the researched email.

    ``bound`` is True only when the orchestrator sets an ``attributed`` verdict.
    ``confidence`` is a 0-100 score used for RANKING only — it never decides
    binding (the boolean rule set in ``scoring.py`` does).
    """

    name: str
    evidence: list[ResearchEvidence] = field(default_factory=list)
    role: str = ""
    role_relevance: bool = False
    confidence: float = 0.0
    local_part_match_level: str = "reject"
    co_occurrence: bool = False
    corroboration_count: int = 0
    authority_evidence: bool = False
    bound: bool = False
    contradictory: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "role_relevance": self.role_relevance,
            "confidence": round(self.confidence, 2),
            "local_part_match_level": self.local_part_match_level,
            "co_occurrence": self.co_occurrence,
            "corroboration_count": self.corroboration_count,
            "authority_evidence": self.authority_evidence,
            "bound": self.bound,
            "contradictory": self.contradictory,
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "PersonCandidate":
        return PersonCandidate(
            name=d.get("name", ""),
            role=d.get("role", ""),
            role_relevance=d.get("role_relevance", False),
            confidence=d.get("confidence", 0.0),
            local_part_match_level=d.get("local_part_match_level", "reject"),
            co_occurrence=d.get("co_occurrence", False),
            corroboration_count=d.get("corroboration_count", 0),
            authority_evidence=d.get("authority_evidence", False),
            bound=d.get("bound", False),
            contradictory=d.get("contradictory", False),
            evidence=[ResearchEvidence.from_dict(e) for e in d.get("evidence", [])],
        )


@dataclass
class ResearchResult:
    """The result of researching ONE email (per-email attribution unit)."""

    email: str
    email_hash: str
    domain: str
    verdict: AttributionVerdict = AttributionVerdict.unattributed
    candidates: list[PersonCandidate] = field(default_factory=list)
    sources_checked: list[str] = field(default_factory=list)
    source_errors: dict[str, str] = field(default_factory=dict)
    attempts: int = 1

    @property
    def bound_candidate(self) -> PersonCandidate | None:
        """The single bound candidate, if the verdict is attributed."""
        if self.verdict is not AttributionVerdict.attributed:
            return None
        for c in self.candidates:
            if c.bound:
                return c
        return None

    @staticmethod
    def from_job(job: dict[str, Any]) -> "ResearchResult":
        """Rebuild a result from a stored ``jobs`` row (for re-score / apply)."""
        return ResearchResult(
            email=job["email"],
            email_hash=job["email_hash"],
            domain=job["domain"],
            verdict=AttributionVerdict(job["verdict"]),
            candidates=[
                PersonCandidate.from_dict(c) for c in json.loads(job["candidates_json"])
            ],
            sources_checked=list(json.loads(job["sources_checked"])),
            source_errors=dict(json.loads(job["source_errors"])),
            attempts=job["attempts"],
        )
