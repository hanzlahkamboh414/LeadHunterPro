"""AI analysis engine – orchestrates AI-driven data enrichment."""

import logging

from app.ai.scorer import CompanyScorer
from app.ai.summarizer import CompanySummarizer

logger = logging.getLogger(__name__)


class AIEngine:
    """Central engine for all AI-driven analyses."""

    def __init__(self) -> None:
        self._scorer = CompanyScorer()
        self._summarizer = CompanySummarizer()

    def score_company(self, raw_data: dict) -> int:
        """Compute a lead score from crawled company data.

        Args:
            raw_data: Crawled website data dictionary.

        Returns:
            Integer score between 0 and 100.
        """
        return self._scorer.calculate(raw_data)

    def summarize_company(self, raw_data: dict) -> str:
        """Generate an AI summary of company data.

        Args:
            raw_data: Crawled website data dictionary.

        Returns:
            The generated summary text.
        """
        return self._summarizer.summarize(raw_data)
