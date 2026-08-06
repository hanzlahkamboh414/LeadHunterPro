"""Address extractor implementation.

Extracts postal addresses from a parsed page. This is the hardest of the
seven extractors because addresses have no reliable marker in plain text.
This implementation uses simple heuristics and patterns.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from app.discovery.website.evidence import ExtractedField, FieldEvidence
from app.discovery.website.extractors import AddressExtractor, ExtractionResult
from app.discovery.website.extractors_impl._confidence_bands import LOW

if TYPE_CHECKING:
    from app.discovery.website.extractors import PageContent

logger = logging.getLogger(__name__)

# Confidence constant: text-based address detection is uncertain
_ADDRESS_CONFIDENCE = LOW

# US state abbreviations (common in construction company addresses)
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}

# Pattern: street address + city, state ZIP
# Example: "123 Main St, Dallas, TX 75201"
_ADDRESS_PATTERN = re.compile(
    r'\b\d+\s+[A-Za-z\s\.]+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd|Way|Court|Ct|Circle|Cir|Parkway|Pkwy|Suite|Ste|Building|Bldg|Unit|#)?,?\s+'
    r'[A-Za-z\s]+,\s*('
    + '|'.join(_US_STATES)
    + r')\s+\d{5}(?:-\d{4})?\b',
    re.IGNORECASE,
)


class HtmlAddressExtractor(AddressExtractor):
    """Extract postal addresses from page text.

    Uses pattern matching to find US-style addresses. This is a
    low-confidence extractor—addresses in plain text are hard to
    distinguish from other content.
    """

    name = "html_address"

    def extract(self, page: PageContent) -> list[ExtractionResult]:
        """Extract addresses from the parsed page.

        Args:
            page: The parsed page to read.

        Returns:
            ExtractionResult objects for addresses found, possibly empty.
        """
        if not page.text_content:
            return []

        results: list[ExtractionResult] = []
        matches = _ADDRESS_PATTERN.finditer(page.text_content)

        for match in matches:
            address = match.group(0).strip()
            evidence = FieldEvidence(
                field=ExtractedField.ADDRESS,
                page_url=page.url,
                method="text_pattern",
                confidence=_ADDRESS_CONFIDENCE,
            )
            result = ExtractionResult(
                value=address,
                evidence=evidence,
            )
            results.append(result)

        logger.debug(
            "HtmlAddressExtractor: %d addresses from %s",
            len(results),
            page.url,
        )
        return results
