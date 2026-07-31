"""AI-powered lead scoring."""

import logging

from app.ai.gateway import AIGateway
from app.ai.prompts.lead_score import build_lead_score_prompt

logger = logging.getLogger(__name__)


class CompanyScorer:
    """Score companies using rule-based heuristics or AI."""

    def __init__(self, use_ai: bool = False) -> None:
        self._use_ai = use_ai
        self._gateway = AIGateway() if use_ai else None

    def calculate(self, data: dict) -> int:
        """Calculate a lead score for the given company data.

        Args:
            data: Crawled website data dictionary.

        Returns:
            Integer score between 0 and 100.
        """
        if self._use_ai and self._gateway is not None:
            prompt = build_lead_score_prompt(data)
            response = self._gateway.ask(prompt)
            try:
                import json
                result = json.loads(response)
                return int(result.get("score", 0))
            except (json.JSONDecodeError, ValueError, KeyError):
                logger.warning("AI scoring failed; falling back to heuristic", exc_info=True)

        score = 0
        if data.get("title"):
            score += 20
        if data.get("description"):
            score += 10
        if data.get("linkedin"):
            score += 20
        if data.get("emails"):
            score += 15
        if data.get("phones"):
            score += 10
        if data.get("contact_page"):
            score += 15
        if data.get("about_page"):
            score += 10
        return min(score, 100)
