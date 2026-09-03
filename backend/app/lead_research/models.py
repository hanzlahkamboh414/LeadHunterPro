"""AI Lead Research — data models (V1, additive).

The full output of researching ONE email+domain is a :class:`LeadDossier`:
refined company, attributed person, buying-intent, timing, fit for The Best
Estimator LLC, and a potential-lead score.

Safety split (spec §5/§6): *facts* carry a ``source_url`` or are marked
``unverified``; *analysis* (intent/timing/fit/score) is reasoned judgment over
the gathered evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AIEvidence:
    """One traceable claim. ``confidence`` is ``verified`` only when a
    ``source_url`` backs the claim; otherwise ``unverified``."""

    claim: str
    source_url: str = ""
    source_type: str = ""
    confidence: str = "verified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "confidence": self.confidence,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AIEvidence":
        return AIEvidence(
            claim=d.get("claim", ""),
            source_url=d.get("source_url", ""),
            source_type=d.get("source_type", ""),
            confidence=d.get("confidence", "verified"),
        )


@dataclass
class CompanyProfile:
    """Cited facts about the company that owns the email."""

    name: str = ""
    industry: str = ""
    location: str = ""
    website: str = ""
    facts: list[AIEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "industry": self.industry,
            "location": self.location,
            "website": self.website,
            "facts": [f.to_dict() for f in self.facts],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "CompanyProfile":
        return CompanyProfile(
            name=d.get("name", ""),
            industry=d.get("industry", ""),
            location=d.get("location", ""),
            website=d.get("website", ""),
            facts=[AIEvidence.from_dict(f) for f in d.get("facts", [])],
        )


@dataclass
class PersonFindings:
    """Person attribution for the email. ``bound`` only when evidence is solid."""

    name: str = ""
    role: str = ""
    role_relevance: bool = False
    bound: bool = False
    evidence: list[AIEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "role_relevance": self.role_relevance,
            "bound": self.bound,
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "PersonFindings":
        return PersonFindings(
            name=d.get("name", ""),
            role=d.get("role", ""),
            role_relevance=d.get("role_relevance", False),
            bound=d.get("bound", False),
            evidence=[AIEvidence.from_dict(e) for e in d.get("evidence", [])],
        )


@dataclass
class IntentAssessment:
    """Does this company need estimation/bidding services? (AI reasoned)."""

    needs_estimation: str = "unknown"  # yes | no | unknown
    signal: str = ""
    reason: str = ""
    evidence: list[AIEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "needs_estimation": self.needs_estimation,
            "signal": self.signal,
            "reason": self.reason,
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "IntentAssessment":
        return IntentAssessment(
            needs_estimation=d.get("needs_estimation", "unknown"),
            signal=d.get("signal", ""),
            reason=d.get("reason", ""),
            evidence=[AIEvidence.from_dict(e) for e in d.get("evidence", [])],
        )


@dataclass
class TimingAssessment:
    """When is the buying window? (AI reasoned)."""

    window: str = "unknown"  # now | soon | later | unknown
    reason: str = ""
    events: list[AIEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "reason": self.reason,
            "events": [e.to_dict() for e in self.events],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TimingAssessment":
        return TimingAssessment(
            window=d.get("window", "unknown"),
            reason=d.get("reason", ""),
            events=[AIEvidence.from_dict(e) for e in d.get("events", [])],
        )


@dataclass
class LeadDossier:
    """The complete researched dossier for one email+domain."""

    email: str
    domain: str
    refined_domain: str = ""
    refined_company: str = ""
    company: CompanyProfile = field(default_factory=CompanyProfile)
    person: PersonFindings = field(default_factory=PersonFindings)
    intent: IntentAssessment = field(default_factory=IntentAssessment)
    timing: TimingAssessment = field(default_factory=TimingAssessment)
    fit: str = ""
    potential_score: float = 0.0
    recommendation: str = "skip"  # contact_now | nurture | skip
    sources_checked: list[str] = field(default_factory=list)
    source_errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "domain": self.domain,
            "refined_domain": self.refined_domain,
            "refined_company": self.refined_company,
            "company": self.company.to_dict(),
            "person": self.person.to_dict(),
            "intent": self.intent.to_dict(),
            "timing": self.timing.to_dict(),
            "fit": self.fit,
            "potential_score": round(self.potential_score, 1),
            "recommendation": self.recommendation,
            "sources_checked": self.sources_checked,
            "source_errors": self.source_errors,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "LeadDossier":
        return LeadDossier(
            email=d.get("email", ""),
            domain=d.get("domain", ""),
            refined_domain=d.get("refined_domain", ""),
            refined_company=d.get("refined_company", ""),
            company=CompanyProfile.from_dict(d.get("company", {})),
            person=PersonFindings.from_dict(d.get("person", {})),
            intent=IntentAssessment.from_dict(d.get("intent", {})),
            timing=TimingAssessment.from_dict(d.get("timing", {})),
            fit=d.get("fit", ""),
            potential_score=d.get("potential_score", 0.0),
            recommendation=d.get("recommendation", "skip"),
            sources_checked=list(d.get("sources_checked", [])),
            source_errors=dict(d.get("source_errors", {})),
        )
