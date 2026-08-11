"""AI analysis engine – orchestrates AI-driven data enrichment."""

import logging
from typing import Any

from app.ai.scorer import CompanyScorer
from app.ai.summarizer import CompanySummarizer

logger = logging.getLogger(__name__)


class AIEngine:
    """Central engine for all AI-driven analyses."""

    def __init__(self) -> None:
        # AI-enabled scorer: qualify() computes the deterministic score first,
        # then asks the configured provider (Combo-LeadHunter via OmniRoute)
        # for a quality verdict, blending both — deterministic fallback on any
        # AI failure.
        self._scorer = CompanyScorer(use_ai=True)
        self._summarizer = CompanySummarizer()

    def score_company(self, raw_data: dict) -> int:
        """Compute the final normalized lead score (0-100).

        Delegates to :meth:`qualify_lead` and returns its final ``score``:
        deterministic when AI is unavailable, blended otherwise.

        Args:
            raw_data: Crawled website data dictionary.

        Returns:
            Integer score between 0 and 100.
        """
        return self.qualify_lead(raw_data)["score"]

    def qualify_lead(self, raw_data: dict, query: dict | None = None) -> dict:
        """Qualify a lead: deterministic score + Combo-LeadHunter AI verdict.

        Args:
            raw_data: Supplied company evidence.
            query: Query context, e.g. ``{"industry": ..., "location": ...}``.

        Returns:
            Qualification metadata dict (see ``CompanyScorer.qualify``) with
            score, deterministic_score, ai_score, ai_confidence,
            justification, qualified, qualification, reasons, strengths,
            concerns, ai_used and error.
        """
        return self._scorer.qualify(raw_data, query)

    def intelligence_for(
        self,
        *,
        record: dict,
        gate: Any,
        query: dict | None = None,
    ) -> dict:
        """Phase 3 — additive AI intelligence for a gate-ACCEPTED record.

        Deterministic verification stays authoritative: this method NEVER
        writes into the ``verification`` namespace (verification_status /
        verification_confidence / business_type / city / state /
        field_evidence / acceptance decision). It only produces ``ai`` (raw
        assessment) and ``qualification`` (final qualification metadata) for a
        consumer to merge additively (Phase 2, H).

        Records the gate did NOT accept (rejected / unknown / unverified) get
        ``{}`` and AI is NEVER consulted for them — so AI can never turn
        rejected → accepted, unknown → accepted, or unverified → accepted.

        AI failure falls back to deterministic scoring metadata (``ai_used``
        False, ``error`` set) and never raises past the deterministic gate.

        Args:
            record: Supplied company evidence.
            gate: An ``AcceptanceResult`` computed deterministically BEFORE
                this call; only ``gate.accepted is True`` records are scored.
            query: Query context (industry/location) — search intent only,
                never company evidence.

        Returns:
            ``{"ai": {...}, "qualification": {...}}`` additive metadata, or
            ``{}`` when the gate did not accept.
        """
        accepted = bool(gate is not None and getattr(gate, "accepted", False))
        if not accepted:
            return {}

        try:
            ai = self.qualify_lead(record, query)
        except Exception as exc:  # noqa: BLE001 — AI must never bypass the gate
            logger.warning(
                "AI intelligence failed; deterministic verdict unchanged: %s",
                exc,
            )
            ai = {
                "score": None,
                "deterministic_score": None,
                "ai_score": None,
                "ai_confidence": None,
                "justification": "",
                "qualified": None,
                "qualification": "",
                "reasons": [],
                "strengths": [],
                "concerns": [],
                "ai_used": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

        return {
            "ai": ai,
            "qualification": {
                "qualified": ai.get("qualified"),
                "score": ai.get("score"),
                "summary": ai.get("qualification", ""),
                "ai_used": ai.get("ai_used", False),
                "error": ai.get("error"),
            },
        }

    def summarize_company(self, raw_data: dict) -> str:
        """Generate an AI summary of company data.

        Args:
            raw_data: Crawled website data dictionary.

        Returns:
            The generated summary text.
        """
        return self._summarizer.summarize(raw_data)
