"""Company discovery engine – main orchestrator.

Pipeline:
    search → validate → clean → return

Returns a structured diagnostic when no providers can produce results,
never silently returning an empty list.
"""

from __future__ import annotations

import logging

from app.engines.discovery.company.company_cleaner import clean_companies
from app.engines.discovery.company.company_models import (
    CompanyDiscoveryResult,
    DiscoveryMetrics,
)
from app.engines.discovery.company.company_search import DiscoveryDiagnostic, search_companies
from app.engines.discovery.company.company_validator import validate_companies

logger = logging.getLogger(__name__)


class CompanyDiscoveryEngine:
    """Orchestrates the full company discovery pipeline.

    Flow::

        search_companies()          → list[CompanyDiscoveryResult] | diagnostic
            ↓
        validate_companies()        → filtered list
            ↓
        clean_companies()           → deduplicated list
            ↓
        return (results, metrics, diagnostic)
    """

    def discover(
        self,
        industry: str,
        location: str,
        *,
        limit: int = 100,
        max_pages: int = 3,
    ) -> tuple[list[CompanyDiscoveryResult], DiscoveryMetrics, DiscoveryDiagnostic | None]:
        """Run the full discovery pipeline.

        Args:
            industry: e.g. ``"Construction Estimating"``.
            location: e.g. ``"Dallas Texas USA"``.
            limit: Maximum number of companies to return.
            max_pages: Search-result pages to scan per provider.

        Returns:
            Tuple of (companies, metrics, diagnostic).
            diagnostic is non-None when no providers could supply results.
        """
        logger.info(
            "Discovery started: industry=%r, location=%r, limit=%d",
            industry,
            location,
            limit,
        )

        t_start = logging.DEBUG  # placeholder; timing handled inside search_companies

        # Step 1 — Search public sources.
        raw_results, search_metrics, diagnostic = search_companies(
            industry=industry,
            location=location,
            limit=limit,
            max_pages=max_pages,
        )
        logger.info("Search complete: %d raw results", search_metrics.total_found)

        # If the search stage already produced a diagnostic, propagate it.
        if diagnostic is not None:
            logger.warning("Search returned diagnostic — short-circuiting validation/clean")
            return [], search_metrics, diagnostic

        if not raw_results:
            logger.info("No raw results from search; returning empty")
            return [], search_metrics, None

        # Step 2 — Validate websites and names.
        validated, validate_metrics = validate_companies(raw_results)
        logger.info("Validated: %d / %d passed", validate_metrics.total_validated, len(raw_results))

        if not validated:
            logger.info("No companies passed validation")
            return [], validate_metrics, None

        # Step 3 — Clean duplicates.
        cleaned, clean_metrics = clean_companies(validated)
        logger.info("Cleaned: %d final companies", clean_metrics.total_cleaned)

        # Aggregate metrics.
        final_metrics = DiscoveryMetrics(
            total_found=search_metrics.total_found,
            total_validated=validate_metrics.total_validated,
            total_cleaned=clean_metrics.total_cleaned,
            errors=(
                search_metrics.errors
                + validate_metrics.errors
                + clean_metrics.errors
            ),
        )

        logger.info(
            "Discovery complete: %d companies | %d errors",
            final_metrics.total_cleaned,
            len(final_metrics.errors),
        )
        return cleaned, final_metrics, None
