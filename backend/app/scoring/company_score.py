"""Company score calculation.

Implements a rule-based scoring engine that rates companies based on the
richness of their publicly-available online profile.
"""

from typing import Any


class CompanyScorer:
    """Score companies by information richness."""

    WEIGHTS: dict[str, int] = {
        "title": 20,
        "description": 10,
        "linkedin": 20,
        "emails": 15,
        "phones": 10,
        "contact_page": 15,
        "about_page": 10,
    }

    def calculate(self, data: dict[str, Any]) -> int:
        """Calculate an aggregate lead score from raw crawled data.

        Args:
            data: Dictionary containing crawled website fields.

        Returns:
            An integer score between 0 and 100.
        """
        score = sum(
            weight for key, weight in self.WEIGHTS.items()
            if data.get(key)
        )
        return min(score, 100)
