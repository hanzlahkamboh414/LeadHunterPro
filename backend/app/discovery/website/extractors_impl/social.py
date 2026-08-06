"""Social media profile extractor implementation.

Extracts social media profile URLs from a parsed page. The HTMLParser has
already identified and categorized social links by platform; this extractor
wraps each one in evidence and produces ExtractionResult objects.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.discovery.website.evidence import ExtractedField, FieldEvidence
from app.discovery.website.extractors import ExtractionResult, SocialExtractor
from app.discovery.website.extractors_impl._confidence_bands import HIGH

if TYPE_CHECKING:
    from app.discovery.website.extractors import PageContent

logger = logging.getLogger(__name__)

# Confidence constant: social links identified by HTMLParser are reliable
# (extracted from known platform patterns)
_SOCIAL_CONFIDENCE = HIGH


class HtmlSocialExtractor(SocialExtractor):
    """Extract social media profile URLs from ParsedPage.social_links.

    The parser has already identified and keyed social links by platform.
    This extractor wraps each in evidence, producing one ExtractionResult
    per profile found.
    """

    name = "html_social"

    def extract(self, page: PageContent) -> list[ExtractionResult]:
        """Extract all social media profiles from the parsed page.

        Args:
            page: The parsed page to read.

        Returns:
            One ExtractionResult per social profile, possibly empty.
        """
        if not page.social_links:
            return []

        results: list[ExtractionResult] = []
        for platform, url in page.social_links.items():
            evidence = FieldEvidence(
                field=ExtractedField.SOCIAL,
                page_url=page.url,
                method="html_parser_pattern",
                confidence=_SOCIAL_CONFIDENCE,
            )
            result = ExtractionResult(
                value=url,
                evidence=evidence,
                attributes={"platform": platform},
            )
            results.append(result)

        logger.debug(
            "HtmlSocialExtractor: %d social profiles from %s",
            len(results),
            page.url,
        )
        return results
