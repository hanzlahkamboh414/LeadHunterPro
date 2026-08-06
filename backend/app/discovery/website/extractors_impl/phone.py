"""Phone number extractor implementation.

Extracts contact phone numbers from a parsed page. The HTMLParser has
already harvested phone numbers via regex; this extractor wraps each
one in evidence and produces ExtractionResult objects.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.discovery.website.evidence import ExtractedField, FieldEvidence
from app.discovery.website.extractors import ExtractionResult, PhoneExtractor
from app.discovery.website.extractors_impl._confidence_bands import HIGH

if TYPE_CHECKING:
    from app.discovery.website.extractors import PageContent

logger = logging.getLogger(__name__)

# Confidence constant: phones harvested by HTMLParser are reliable
# (extracted from tel: links or validated patterns)
_PHONE_CONFIDENCE = HIGH


class HtmlPhoneExtractor(PhoneExtractor):
    """Extract phone numbers from ParsedPage.phones.

    The parser has already identified and validated phone numbers.
    This extractor wraps each in evidence, producing one ExtractionResult
    per phone found.
    """

    name = "html_phone"

    def extract(self, page: PageContent) -> list[ExtractionResult]:
        """Extract all phone numbers from the parsed page.

        Args:
            page: The parsed page to read.

        Returns:
            One ExtractionResult per phone, possibly empty.
        """
        if not page.phones:
            return []

        results: list[ExtractionResult] = []
        for phone in page.phones:
            evidence = FieldEvidence(
                field=ExtractedField.PHONE,
                page_url=page.url,
                method="html_parser_regex",
                confidence=_PHONE_CONFIDENCE,
            )
            result = ExtractionResult(
                value=phone,
                evidence=evidence,
            )
            results.append(result)

        logger.debug(
            "HtmlPhoneExtractor: %d phones from %s",
            len(results),
            page.url,
        )
        return results
