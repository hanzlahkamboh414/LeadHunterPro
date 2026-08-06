"""Services/trades extractor implementation.

Extracts the services or trades a company offers from a parsed page.
Draws on multiple signals: page text, meta keywords, h1 headings, and
meta description. Returns one result per service identified.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from app.discovery.website.evidence import ExtractedField, FieldEvidence
from app.discovery.website.extractors import ExtractionResult, ServicesExtractor
from app.discovery.website.extractors_impl._confidence_bands import LOW, MEDIUM

if TYPE_CHECKING:
    from app.discovery.website.extractors import PageContent

logger = logging.getLogger(__name__)

# Confidence constants
_KEYWORDS_CONFIDENCE = MEDIUM  # Meta keywords are explicit
_TEXT_CONFIDENCE = LOW  # Text patterns are uncertain

# Common construction trades/services
_CONSTRUCTION_SERVICES = {
    "roofing",
    "plumbing",
    "electrical",
    "hvac",
    "concrete",
    "painting",
    "flooring",
    "landscaping",
    "framing",
    "drywall",
    "carpentry",
    "masonry",
    "excavation",
    "demolition",
    "steel",
    "welding",
    "general contractor",
    "commercial construction",
    "residential construction",
    "remodeling",
    "renovation",
}

# Pattern to find services in text
_SERVICE_PATTERN = re.compile(
    r'\b('
    + '|'.join(re.escape(s) for s in _CONSTRUCTION_SERVICES)
    + r')\b',
    re.IGNORECASE,
)


class HtmlServicesExtractor(ServicesExtractor):
    """Extract services/trades from keywords, h1, description, and text.

    Meta keywords provide the strongest signal. Services mentioned in
    h1 headings and meta description are also extracted. Text content
    is scanned for known construction service keywords.
    """

    name = "html_services"

    def extract(self, page: PageContent) -> list[ExtractionResult]:
        """Extract services from the parsed page.

        Args:
            page: The parsed page to read.

        Returns:
            One ExtractionResult per service found, possibly empty.
        """
        results: list[ExtractionResult] = []
        seen: set[str] = set()

        # Extract from meta keywords
        if page.meta_keywords:
            for keyword in page.meta_keywords.split(","):
                service = keyword.strip().lower()
                if (
                    service
                    and service in _CONSTRUCTION_SERVICES
                    and service not in seen
                ):
                    evidence = FieldEvidence(
                        field=ExtractedField.SERVICES,
                        page_url=page.url,
                        method="meta_keywords",
                        confidence=_KEYWORDS_CONFIDENCE,
                    )
                    result = ExtractionResult(value=service, evidence=evidence)
                    results.append(result)
                    seen.add(service)

        # Extract from h1 headings
        for h1_text in page.h1_texts:
            matches = _SERVICE_PATTERN.findall(h1_text.lower())
            for service in matches:
                if service not in seen:
                    evidence = FieldEvidence(
                        field=ExtractedField.SERVICES,
                        page_url=page.url,
                        method="html_h1",
                        confidence=_TEXT_CONFIDENCE,
                    )
                    result = ExtractionResult(value=service, evidence=evidence)
                    results.append(result)
                    seen.add(service)

        # Extract from description
        if page.description:
            matches = _SERVICE_PATTERN.findall(page.description.lower())
            for service in matches:
                if service not in seen:
                    evidence = FieldEvidence(
                        field=ExtractedField.SERVICES,
                        page_url=page.url,
                        method="meta_description",
                        confidence=_TEXT_CONFIDENCE,
                    )
                    result = ExtractionResult(value=service, evidence=evidence)
                    results.append(result)
                    seen.add(service)

        # Extract from text content (sample first 2000 chars)
        if page.text_content:
            text_sample = page.text_content[:2000].lower()
            matches = _SERVICE_PATTERN.findall(text_sample)
            for service in matches:
                if service not in seen:
                    evidence = FieldEvidence(
                        field=ExtractedField.SERVICES,
                        page_url=page.url,
                        method="text_pattern",
                        confidence=_TEXT_CONFIDENCE,
                    )
                    result = ExtractionResult(value=service, evidence=evidence)
                    results.append(result)
                    seen.add(service)

        logger.debug(
            "HtmlServicesExtractor: %d services from %s",
            len(results),
            page.url,
        )
        return results
