"""Extractor manager — orchestrates field extraction across all extractors.

The ExtractorManager loads all 7 field extractor implementations and
runs them over a parsed page, collecting their results. This is the
single entry point for extraction in the discovery pipeline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.discovery.website.extractors import ExtractionResult
from app.discovery.website.extractors_impl import (
    HtmlAddressExtractor,
    HtmlCompanyNameExtractor,
    HtmlEmailExtractor,
    HtmlLeadershipExtractor,
    HtmlPhoneExtractor,
    HtmlServicesExtractor,
    HtmlSocialExtractor,
)

if TYPE_CHECKING:
    from app.discovery.website.extractors import FieldExtractor, PageContent

logger = logging.getLogger(__name__)


class ExtractorManager:
    """Manages and executes all field extractors.

    Loads the 7 concrete extractor implementations and runs them over
    parsed pages. Extractors are executed independently; failures in
    one extractor do not block others.
    """

    def __init__(self) -> None:
        """Initialize the extractor manager with all extractors."""
        self._extractors: list[FieldExtractor] = self._load_extractors()
        logger.info(
            "ExtractorManager initialized with %d extractors",
            len(self._extractors),
        )

    def _load_extractors(self) -> list[FieldExtractor]:
        """Load all concrete extractor implementations.

        Returns:
            List of instantiated extractors, one per field.
        """
        return [
            HtmlCompanyNameExtractor(),
            HtmlPhoneExtractor(),
            HtmlEmailExtractor(),
            HtmlAddressExtractor(),
            HtmlLeadershipExtractor(),
            HtmlSocialExtractor(),
            HtmlServicesExtractor(),
        ]

    def extract_all(self, page: PageContent) -> list[ExtractionResult]:
        """Extract all fields from a parsed page.

        Runs all extractors over the page. Each extractor is isolated;
        a failure in one does not prevent others from running.

        Args:
            page: The parsed page to extract from.

        Returns:
            Combined extraction results from all extractors, possibly empty.
        """
        all_results: list[ExtractionResult] = []

        for extractor in self._extractors:
            try:
                results = extractor.extract(page)
                all_results.extend(results)
                logger.debug(
                    "%s: %d results from %s",
                    extractor.extractor_name,
                    len(results),
                    page.url,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Extractor %s failed on %s: %s",
                    extractor.extractor_name,
                    page.url,
                    exc,
                )
                # Continue with other extractors
                continue

        logger.info(
            "ExtractorManager: %d total results from %s",
            len(all_results),
            page.url,
        )
        return all_results

    def get_extractors(self) -> list[FieldExtractor]:
        """Get all loaded extractors.

        Returns:
            List of extractor instances.
        """
        return self._extractors.copy()

    def describe(self) -> dict[str, Any]:
        """Describe all loaded extractors for diagnostics.

        Returns:
            Dict with ``"extractors"`` (one ``describe()`` mapping per
            extractor) and ``"count"`` (how many are loaded).
        """
        return {
            "extractors": [e.describe() for e in self._extractors],
            "count": len(self._extractors),
        }
