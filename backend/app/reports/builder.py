"""Report builder for company research summaries."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ReportBuilder:
    """Assemble a human-readable research report from a company summary."""

    def build(self, company: dict[str, Any], summary: dict[str, Any]) -> str:
        """Build a text report from company data and AI summary.

        Args:
            company: Company metadata dictionary.
            summary: AI-generated summary dictionary.

        Returns:
            A formatted plain-text report string.
        """
        lines = [
            f"Company: {company.get('company_name', 'N/A')}",
            f"Website: {company.get('website', 'N/A')}",
            f"Industry: {company.get('industry', 'N/A')}",
            "",
            "AI Summary:",
            summary.get("summary", "No summary available."),
        ]
        return "\n".join(lines)
