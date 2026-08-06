"""Email address extractor implementation.

Extracts contact email addresses from a parsed page. The HTMLParser has
already harvested emails via regex; this extractor wraps each one in
evidence and produces ExtractionResult objects.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.discovery.website.evidence import ExtractedField, FieldEvidence
from app.discovery.website.extractors import EmailExtractor, ExtractionResult
from app.discovery.website.extractors_impl._confidence_bands import HIGH

if TYPE_CHECKING:
    from app.discovery.website.extractors import PageContent

logger = logging.getLogger(__name__)

# Confidence constant: emails harvested by HTMLParser are reliable
# (extracted from mailto: links or validated patterns)
_EMAIL_CONFIDENCE = HIGH


class HtmlEmailExtractor(EmailExtractor):
    """Extract email addresses from ParsedPage.emails.

    The parser has already identified and validated emails. This extractor
    wraps each in evidence, producing one ExtractionResult per email found.
    """

    name = "html_email"

    def extract(self, page: PageContent) -> list[ExtractionResult]:
        """Extract all email addresses from the parsed page.

        Args:
            page: The parsed page to read.

        Returns:
            One ExtractionResult per email, possibly empty.
        """
        if not page.emails:
            return []

        results: list[ExtractionResult] = []
        for email in page.emails:
            evidence = FieldEvidence(
                field=ExtractedField.EMAIL,
                page_url=page.url,
                method="html_parser_regex",
                confidence=_EMAIL_CONFIDENCE,
            )
            result = ExtractionResult(
                value=email,
                evidence=evidence,
            )
            results.append(result)

        logger.debug(
            "HtmlEmailExtractor: %d emails from %s",
            len(results),
            page.url,
        )
        return results
