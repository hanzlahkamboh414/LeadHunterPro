"""AI Lead Research — Scoring + Recommendation (Stage 4).

Deterministic signal-based scoring — NO LLM call required.

Each lead attribute contributes a weighted signal to the final score.
The deterministic gate is the FINAL authority on recommendation.

Credit optimization: replaced LLM scoring with rule-based system
(~20 LLM calls saved per job, ~25% credit reduction).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.lead_research.models import (
    AIEvidence,
    CompanyProfile,
    IntentAssessment,
    LeadDossier,
    PersonFindings,
    TimingAssessment,
)

logger = logging.getLogger(__name__)


class AIAskFn(Protocol):
    def __call__(self, prompt: str) -> str: ...


# Construction-related industry keywords
_CONSTRUCTION_KEYWORDS = (
    "contractor", "construction", "general contractor", "subcontractor",
    "gc", "builder", "building", "estimating", "preconstruction",
    "develop", "developer", "site work", "paving", "concrete", "asphalt",
    "excavat", "earthwork", "demolition", "framing", "roofing", "electrical",
    "plumbing", "mechanical", "hvac", "masonry", "steel", "civil",
    "engineering", "grading", "landscaping", "insulation", "drywall",
    "painting", "flooring", "glazing", "waterproofing",
)


def _is_construction(industry: str) -> bool:
    """Return True if industry string indicates construction-related work."""
    ind = (industry or "").lower()
    return any(kw in ind for kw in _CONSTRUCTION_KEYWORDS)


def _is_service_area(location: str) -> bool:
    """Return True if location is in our primary service area (Texas)."""
    loc = (location or "").lower()
    return "texas" in loc or "tx" in loc


class LeadScorer:
    """Stage 4: deterministic signal-based scoring.

    Each lead attribute contributes a weighted signal:
    - Construction industry:      +2.0
    - Person bound (reachable):   +1.5
- Person has relevant role:   +1.0
    - Needs estimation (yes):     +2.0
    - Intent signal present:      +1.0
    - Timing window present:      +0.5
    - Service area (Texas):       +0.5
    - Has evidence facts:         +0.5
    - Non-construction industry:  -1.0

    Max possible: 9.0  |  Min possible: -1.0
    """

    def __init__(self, *, ai_ask: AIAskFn | None = None) -> None:
        # Kept for backward compatibility (tests inject ai_ask).
        # The deterministic scorer does not use it.
        self._ai_ask = ai_ask

    def score(
        self,
        email: str,
        company: CompanyProfile,
        person: PersonFindings,
        intent: IntentAssessment,
        timing: TimingAssessment,
    ) -> tuple[str, float, str]:
        """Score the lead deterministically. Returns (fit, potential_score, recommendation)."""
        signals: list[tuple[str, float]] = []

        # --- Company signals ---
        if company.name:
            if _is_construction(company.industry):
                signals.append(("construction industry", 2.0))
            elif company.industry:
                signals.append(("related industry", 0.5))
            else:
                signals.append(("company identified", 0.5))
        else:
            signals.append(("no company info", -1.0))

        # Non-construction penalty
        if company.industry and not _is_construction(company.industry):
            signals.append(("non-construction", -1.0))

        # --- Person signals ---
        if person.bound:
            signals.append(("reachable person", 1.5))
        if person.name and person.role_relevance:
            signals.append(("relevant role", 1.0))

        # --- Intent signals ---
        if (intent.needs_estimation or "").lower() in ("yes", "likely", "probable"):
            signals.append(("needs estimation", 2.0))
        if intent.signal:
            signals.append(("intent signal", 1.0))

        # --- Timing signals ---
        if timing.window:
            signals.append(("timing window", 0.5))

        # --- Location signal ---
        if _is_service_area(company.location):
            signals.append(("service area", 0.5))

        # --- Evidence signal ---
        if company.facts:
            signals.append(("has evidence", 0.5))

        # Calculate score
        raw_score = sum(val for _, val in signals)
        potential_score = round(max(-1.0, min(10.0, raw_score)), 1)

        # Generate fit description
        fit = self._build_fit_text(signals, company, person)

        # Deterministic gate (hard rules — construction-only, relevant role,
        # verifiable location required)
        recommendation = self._gate(person, potential_score, company.industry, company.location)

        logger.info(
            "Scoring %s: score=%.1f rec=%s signals=%s",
            email, potential_score, recommendation,
            [s[0] for s in signals],
        )

        return fit, potential_score, recommendation

    def _build_fit_text(
        self,
        signals: list[tuple[str, float]],
        company: CompanyProfile,
        person: PersonFindings,
    ) -> str:
        """Generate a human-readable fit assessment from signals."""
        if not signals:
            return "Insufficient data to assess fit."

        pos = [s[0] for s in signals if s[1] > 0]
        neg = [s[0] for s in signals if s[1] < 0]

        parts: list[str] = []
        if company.name:
            parts.append(company.name)
        if pos:
            parts.append("identified with " + ", ".join(pos[:3]))
        if neg:
            parts.append("but " + ", ".join(neg))

        return ". ".join(parts) + "." if parts else "Partial information available."

    def _gate(
        self,
        person: PersonFindings,
        potential_score: float,
        industry: str = "",
        location: str = "",
    ) -> str:
        """Deterministic gate — final authority on recommendation.

        HARD RULES (founder):
        - A KNOWN non-construction company is NEVER a qualified lead → skip
          (The Best Estimator sells to construction — an IT company is worthless).
        - contact_now requires score >= 6.0 AND a bound person AND a relevant
          role (decision-maker who owns estimation/bidding — not an IT/support
          contact). Without a relevant role, even a high score caps at nurture.
        - contact_now also requires a non-empty, verified location — never call
          a decision-maker we cannot geolocate (nsarro@unitedcr.com bug:
          fabricated "Insurance Restoration Contractor" + no location = 9.0
          contact_now, which is wrong).
        - nurture: score >= 3.0
        - skip: below 3.0
        """
        ind = (industry or "").lower()
        # Hard reject: explicitly non-construction industry (never contact_now
        # even with a high score — the -1.0 signal penalty alone is not enough).
        if ind and not _is_construction(ind):
            return "skip"

        # Location gate: contact_now requires a verified location string.
        loc = (location or "").strip()
        if potential_score >= 6.0 and person.bound and person.role_relevance:
            if not loc:
                logger.info("Location gate: score %.1f contact_now downgraded to nurture (no verified location)", potential_score)
                return "nurture"
            return "contact_now"
        elif potential_score >= 3.0:
            return "nurture"
        else:
            return "skip"

    def _default_score(self, person: PersonFindings) -> tuple[str, float, str]:
        """Default score when scoring fails."""
        if person.bound and person.name:
            return ("Partial info — person identified but scoring unavailable", 3.0, "nurture")
        return ("Unable to assess — scoring unavailable", 0.0, "skip")


def regate_recommendation(dossier: LeadDossier) -> str:
    """Re-derive a STORED dossier's recommendation from TODAY'S hard rules.

    Old dossiers carry research-time recommendations — researched before the
    deterministic role override and the non-construction hard skip existed, an
    AI could return contact_now for a "Sales Representative" or "Contact /
    Representative" at Ferguson. Without re-researching (zero credits), we
    re-apply the SAME gates the live scorer uses:

      1. Industry construction check        -> hard skip
      2. role_relevance recomputed from the ROLE STRING (the deterministic
         list is the authority, never the stored AI-era flag)
      3. bound + score thresholds

    Applied at read time in :func:`app.api.v1.leads.list_leads`, so the leads
    page is always an honest view of current rules — never a stale AI guess.
    """
    from app.engines.lead.lead_models import role_is_plausibly_relevant

    ind = (dossier.company.industry or "").lower()
    if ind and not _is_construction(ind):
        return "skip"

    relevant = bool((dossier.person.role or "").strip()) and role_is_plausibly_relevant(
        dossier.person.role
    )
    loc = (dossier.company.location or "").strip()
    if dossier.potential_score >= 6.0 and dossier.person.bound and relevant:
        if not loc:
            return "nurture"
        return "contact_now"
    if dossier.potential_score >= 3.0:
        return "nurture"
    return "skip"
