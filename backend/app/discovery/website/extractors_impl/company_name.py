"""Company name extractor implementation.

Extracts the company's own name from a parsed page. Draws on multiple
signals: page title, h1 headings, and meta description. Strips common
boilerplate (e.g., "Acme Inc | Home") to extract the core name.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from app.discovery.website.evidence import ExtractedField, FieldEvidence
from app.discovery.website.extractors import CompanyNameExtractor, ExtractionResult
from app.discovery.website.extractors_impl._confidence_bands import HIGH, LOW, MEDIUM

if TYPE_CHECKING:
    from app.discovery.website.extractors import PageContent

logger = logging.getLogger(__name__)

# Confidence constants
_TITLE_CONFIDENCE = HIGH  # Page title is authoritative
_H1_CONFIDENCE = MEDIUM  # H1 headings often contain the name
_DESCRIPTION_CONFIDENCE = LOW  # Description may mention it

# Boilerplate patterns to strip from titles
_TITLE_SEPARATORS = re.compile(r"\s*[|•·–—-]\s*")
_COMMON_SUFFIXES = (
    "home",
    "homepage",
    "welcome",
    "index",
    "official site",
    "official website",
)


class HtmlCompanyNameExtractor(CompanyNameExtractor):
    """Extract company name from title, h1, and description.

    The page title is the strongest signal. H1 headings and meta
    description provide additional candidates. Common boilerplate
    (e.g., " | Home") is stripped.
    """

    name = "html_company_name"

    def extract(self, page: PageContent) -> list[ExtractionResult]:
        """Extract company name from the parsed page.

        Args:
            page: The parsed page to read.

        Returns:
            ExtractionResult objects from title, h1, and description.
        """
        results: list[ExtractionResult] = []

        # Extract from title
        if page.title:
            names = self._extract_from_title(page.title)
            for name in names:
                evidence = FieldEvidence(
                    field=ExtractedField.COMPANY_NAME,
                    page_url=page.url,
                    method="html_title",
                    confidence=_TITLE_CONFIDENCE,
                )
                result = ExtractionResult(value=name, evidence=evidence)
                results.append(result)

        # Extract from h1 headings
        for h1_text in page.h1_texts:
            if h1_text and len(h1_text.strip()) > 2:
                evidence = FieldEvidence(
                    field=ExtractedField.COMPANY_NAME,
                    page_url=page.url,
                    method="html_h1",
                    confidence=_H1_CONFIDENCE,
                )
                result = ExtractionResult(
                    value=h1_text.strip(), evidence=evidence
                )
                results.append(result)

        # Extract from meta description (first sentence)
        if page.description:
            first_sentence = page.description.split(".")[0].strip()
            if first_sentence and len(first_sentence) > 5:
                evidence = FieldEvidence(
                    field=ExtractedField.COMPANY_NAME,
                    page_url=page.url,
                    method="meta_description",
                    confidence=_DESCRIPTION_CONFIDENCE,
                )
                result = ExtractionResult(
                    value=first_sentence, evidence=evidence
                )
                results.append(result)

        logger.debug(
            "HtmlCompanyNameExtractor: %d candidates from %s",
            len(results),
            page.url,
        )
        return results

    def _extract_from_title(self, title: str) -> list[str]:
        """Extract company name candidates from a page title.

        Splits on common separators (| - •) and strips boilerplate suffixes.

        Args:
            title: The page title text.

        Returns:
            List of name candidates, possibly empty.
        """
        # Split by separator
        parts = _TITLE_SEPARATORS.split(title)
        candidates: list[str] = []

        for part in parts:
            cleaned = part.strip()
            if not cleaned or len(cleaned) < 3:
                continue

            # Skip common boilerplate
            if cleaned.lower() in _COMMON_SUFFIXES:
                continue

            candidates.append(cleaned)

        return candidates
