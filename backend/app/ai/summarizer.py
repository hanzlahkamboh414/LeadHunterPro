"""AI-powered company summarization."""

import logging

from app.ai.gateway import AIGateway
from app.ai.prompts.company_summary import build_company_summary_prompt

logger = logging.getLogger(__name__)


class CompanySummarizer:
    """Generate AI summaries from crawled company data."""

    def __init__(self, gateway: AIGateway | None = None) -> None:
        self._gateway = gateway or AIGateway()

    def summarize(self, raw_data: dict) -> str:
        """Generate an AI summary of the company from raw crawled data.

        Args:
            raw_data: Dictionary of crawled website fields.

        Returns:
            The AI-generated summary string.
        """
        prompt = build_company_summary_prompt(raw_data)
        return self._gateway.ask(prompt)
