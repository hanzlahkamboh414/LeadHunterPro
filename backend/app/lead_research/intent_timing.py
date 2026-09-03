"""AI Lead Research — Intent + Timing Analysis (Stage 3).

Purely AI-reasoned: takes gathered company facts + person findings and
assesses whether the company likely needs preconstruction/estimation
services, and when the buying window is.

Safety rules enforced:
- Reasoning must be based on provided facts — no assumed facts.
- "unknown" when information is insufficient — never guess.
- Every supporting claim should cite a source_url where possible.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Protocol

from app.lead_research.models import AIEvidence, CompanyProfile, IntentAssessment, PersonFindings, TimingAssessment
from app.lead_research.prompts import intent_timing_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

class AIAskFn(Protocol):
    def __call__(self, prompt: str) -> str: ...


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

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


def _dict_to_evidence(d: dict[str, Any]) -> AIEvidence:
    return AIEvidence(
        claim=d.get("claim", ""),
        source_url=d.get("source_url", ""),
        source_type=d.get("source_type", ""),
        confidence=d.get("confidence", "unverified"),
    )


# ---------------------------------------------------------------------------
# IntentTimingAnalyzer
# ---------------------------------------------------------------------------

class IntentTimingAnalyzer:
    """Stage 3: buying-intent + timing analysis (AI reasoned).

    Takes a CompanyProfile + PersonFindings, builds a facts summary,
    and asks the AI to assess need + timing.
    """

    def __init__(self, *, ai_ask: AIAskFn | None = None) -> None:
        self._ai_ask = ai_ask

    def _get_ai_ask(self) -> AIAskFn:
        if self._ai_ask is None:
            from app.ai.gateway import AIGateway
            self._ai_ask = AIGateway().ask
        return self._ai_ask

    def analyze(
        self,
        email: str,
        company: CompanyProfile,
        person: PersonFindings,
    ) -> tuple[IntentAssessment, TimingAssessment]:
        """Analyze buying intent and timing for this lead.

        Returns (intent, timing) tuple.
        """
        # Build facts summary from company + person
        facts_summary = self._build_facts_summary(company, person)

        prompt = intent_timing_prompt(
            email=email,
            company_name=company.name,
            industry=company.industry,
            facts_summary=facts_summary,
        )

        ai_fn = self._get_ai_ask()
        try:
            raw = ai_fn(prompt)
        except Exception as exc:
            logger.error("AI call failed for %s: %s", email, exc)
            return (
                IntentAssessment(
                    needs_estimation="unknown",
                    reason=f"AI call failed: {exc}",
                ),
                TimingAssessment(
                    window="unknown",
                    reason=f"AI call failed: {exc}",
                ),
            )

        data = _parse_ai_json(raw)
        if not data:
            return (
                IntentAssessment(
                    needs_estimation="unknown",
                    reason="AI returned unparseable response",
                ),
                TimingAssessment(
                    window="unknown",
                    reason="AI returned unparseable response",
                ),
            )

        # Parse intent
        intent = IntentAssessment(
            needs_estimation=data.get("needs_estimation", "unknown"),
            signal=data.get("signal", ""),
            reason=data.get("reason", ""),
            evidence=[_dict_to_evidence(e) for e in data.get("evidence", []) if isinstance(e, dict)],
        )

        # Parse timing
        timing = TimingAssessment(
            window=data.get("timing_window", "unknown"),
            reason=data.get("timing_reason", ""),
            events=[_dict_to_evidence(e) for e in data.get("timing_events", []) if isinstance(e, dict)],
        )

        return intent, timing

    def _build_facts_summary(
        self,
        company: CompanyProfile,
        person: PersonFindings,
    ) -> str:
        """Build a human-readable facts summary for the prompt."""
        lines = []

        if company.name:
            lines.append(f"Company: {company.name}")
        if company.industry:
            lines.append(f"Industry: {company.industry}")
        if company.location:
            lines.append(f"Location: {company.location}")
        if company.website:
            lines.append(f"Website: {company.website}")

        # Company facts
        if company.facts:
            lines.append("\nCompany facts:")
            for f in company.facts:
                prefix = "[verified]" if f.confidence == "verified" else "[unverified]"
                lines.append(f"  {prefix} {f.claim}")

        # Person findings
        if person.name:
            lines.append(f"\nContact person: {person.name} ({person.role})")
            lines.append(f"  Role relevant to estimation: {person.role_relevance}")
            lines.append(f"  Bound to email: {person.bound}")
        elif person.evidence:
            lines.append("\nPerson: not conclusively identified")
            for e in person.evidence:
                prefix = "[verified]" if e.confidence == "verified" else "[unverified]"
                lines.append(f"  {prefix} {e.claim}")

        if not lines:
            return "(no facts available)"

        return "\n".join(lines)
