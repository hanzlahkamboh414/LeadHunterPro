"""Company Discovery Engine — main orchestrator.

Searches public sources (Google, Bing), validates websites, cleans
duplicates, and returns structured lead candidates.

This engine does NOT use AI. It is purely rules-based.
"""

from __future__ import annotations

import logging

from app.engines.discovery.company.company_cleaner import clean_companies
from app.engines.discovery.company.company_models import (
    CompanyDiscoveryResult,
    DiscoveryMetrics,
)
from app.engines.discovery.company.company_search import search_companies
from app.engines.discovery.company.company_validator import validate_companies

logger = logging.getLogger(__name__)


class CompanyDiscoveryEngine:
    """Orchestrates the full company discovery pipeline.

    Flow::

        search_companies()
            ↓
        validate_companies()
            ↓
        clean_companies()
            ↓
        return list[CompanyDiscoveryResult]
    """

    def discover(
        self,
        industry: str,
        location: str,
        *,
        limit: int = 100,
        max_pages: int = 3,
    ) -> tuple[list[CompanyDiscoveryResult], DiscoveryMetrics]:
        """Run the full discovery pipeline.

        Args:
            industry: e.g. ``"Construction Estimating"``.
            location: e.g. ``"Dallas Texas USA"``.
            limit: Maximum number of companies to return.
            max_pages: Search-result pages to scan per source.

        Returns:
            Tuple of (discovered companies, run metrics).
        """
        logger.info(
            "Discovery started: industry=%r, location=%r, limit=%d",
            industry,
            location,
            limit,
        )

        # Step 1 — Search public sources.
        raw_results, search_metrics = search_companies(
            industry=industry,
            location=location,
            limit=limit,
            max_pages=max_pages,
        )
        logger.info("Sources queried: %d companies found", search_metrics.total_found)

        if not raw_results:
            logger.info("No raw results from search; returning empty")
            return [], search_metrics

        # Step 2 — Validate websites and names.
        validated, validate_metrics = validate_companies(raw_results)
        logger.info("Validated: %d / %d passed", validate_metrics.total_validated, len(raw_results))

        if not validated:
            logger.info("No companies passed validation")
            return [], validate_metrics

        # Step 3 — Clean duplicates.
        cleaned, clean_metrics = clean_companies(validated)
        logger.info("Cleaned: %d final companies", clean_metrics.total_cleaned)

        # Aggregate metrics.
        final_metrics = DiscoveryMetrics(
            total_found=search_metrics.total_found,
            total_validated=validate_metrics.total_validated,
            total_cleaned=clean_metrics.total_cleaned,
            errors=search_metrics.errors
            + validate_metrics.errors
            + clean_metrics.errors,
        )

        logger.info(
            "Discovery complete: %d companies | %d errors",
            final_metrics.total_cleaned,
            len(final_metrics.errors),
        )
        return cleaned, final_metrics
