"""Leadership/decision-maker extractor implementation.

Extracts decision-makers (owners, principals, executives) from a parsed page.
Uses simple pattern matching on text content to find names paired with
title indicators.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from app.discovery.website.evidence import ExtractedField, FieldEvidence
from app.discovery.website.extractors import ExtractionResult, LeadershipExtractor
from app.discovery.website.extractors_impl._confidence_bands import LOW

if TYPE_CHECKING:
    from app.discovery.website.extractors import PageContent

logger = logging.getLogger(__name__)

# Confidence constant: text-based leadership detection is uncertain
_LEADERSHIP_CONFIDENCE = LOW

# Common leadership titles.
# Order matters: alternation is leftmost-wins, so a longer title must not
# be shadowed by a shorter one that prefixes it.
_TITLE_PATTERNS = (
    "CEO", "President", "Owner", "Principal", "Founder",
    "Managing Director", "Executive", "VP", "Vice President",
    "Partner", "Director",
)

# Pattern: Name followed by title
# Example: "John Smith, CEO" or "Jane Doe - President"
_LEADERSHIP_PATTERN = re.compile(
    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*[,\-–—:]\s*('
    + '|'.join(_TITLE_PATTERNS)
    + r')',
    re.IGNORECASE,
)


class HtmlLeadershipExtractor(LeadershipExtractor):
    """Extract leadership/decision-maker information from page text.

    Uses pattern matching to find names paired with executive titles.
    This is a low-confidence extractor—leadership mentions in plain
    text are contextual and may not be current.
    """

    name = "html_leadership"

    def extract(self, page: PageContent) -> list[ExtractionResult]:
        """Extract leadership information from the parsed page.

        Args:
            page: The parsed page to read.

        Returns:
            ExtractionResult objects for leaders found, possibly empty.
        """
        if not page.text_content:
            return []

        results: list[ExtractionResult] = []
        matches = _LEADERSHIP_PATTERN.finditer(page.text_content)

        for match in matches:
            name = match.group(1).strip()
            title = match.group(2).strip()
            evidence = FieldEvidence(
                field=ExtractedField.LEADERSHIP,
                page_url=page.url,
                method="text_pattern",
                confidence=_LEADERSHIP_CONFIDENCE,
            )
            result = ExtractionResult(
                value=name,
                evidence=evidence,
                attributes={"title": title},
            )
            results.append(result)

        logger.debug(
            "HtmlLeadershipExtractor: %d leaders from %s",
            len(results),
            page.url,
        )
        return results
