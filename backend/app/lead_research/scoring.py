"""AI Lead Research — Scoring + Recommendation (Stage 4).

Combines AI-reasoned fit assessment with a deterministic gate to produce
a potential-lead score and recommendation (contact_now / nurture / skip).

The deterministic gate is the FINAL authority on "qualified" — the AI score
never bends it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

from app.lead_research.models import (
    AIEvidence,
    CompanyProfile,
    IntentAssessment,
    LeadDossier,
    PersonFindings,
    TimingAssessment,
)
from app.lead_research.prompts import fit_scoring_prompt

logger = logging.getLogger(__name__)


class AIAskFn(Protocol):
    def __call__(self, prompt: str) -> str: ...


def _parse_ai_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse AI JSON response (length=%d)", len(raw))
        return {}


class LeadScorer:
    """Stage 4: fit assessment + potential score + recommendation.

    AI proposes a score; a deterministic gate validates the recommendation.
    """

    def __init__(self, *, ai_ask: AIAskFn | None = None) -> None:
        self._ai_ask = ai_ask

    def _get_ai_ask(self) -> AIAskFn:
        if self._ai_ask is None:
            from app.ai.gateway import AIGateway
            self._ai_ask = AIGateway().ask
        return self._ai_ask

    def score(
        self,
        email: str,
        company: CompanyProfile,
        person: PersonFindings,
        intent: IntentAssessment,
        timing: TimingAssessment,
    ) -> tuple[str, float, str]:
        """Score the lead. Returns (fit, potential_score, recommendation)."""
        prompt = fit_scoring_prompt(
            email=email,
            company_name=company.name,
            industry=company.industry,
            location=company.location,
            person_name=person.name,
            person_role=person.role,
            intent_needs_estimation=intent.needs_estimation,
            intent_signal=intent.signal,
            timing_window=timing.window,
        )

        ai_fn = self._get_ai_ask()
        try:
            raw = ai_fn(prompt)
        except Exception as exc:
            logger.error("AI scoring call failed for %s: %s", email, exc)
            return self._default_score(person)

        data = _parse_ai_json(raw)
        if not data:
            return self._default_score(person)

        fit = data.get("fit", "")
        potential_score = float(data.get("potential_score", 0.0))
        recommendation = data.get("recommendation", "skip")

        # Deterministic gate override
        recommendation = self._gate(person, potential_score, recommendation)

        return fit, potential_score, recommendation

    def _gate(
        self,
        person: PersonFindings,
        potential_score: float,
        ai_recommendation: str,
    ) -> str:
        """Deterministic gate — final authority on recommendation.

        Rules:
        - contact_now: score >= 6.0 AND person.bound == True
        - nurture: score >= 3.0
        - skip: below 3.0
        """
        if potential_score >= 6.0 and person.bound:
            return "contact_now"
        elif potential_score >= 3.0:
            return "nurture"
        else:
            return "skip"

    def _default_score(self, person: PersonFindings) -> tuple[str, float, str]:
        """Default score when AI fails."""
        if person.bound and person.name:
            return ("Partial info — person identified but AI unavailable", 3.0, "nurture")
        return ("Unable to assess — AI unavailable", 0.0, "skip")
