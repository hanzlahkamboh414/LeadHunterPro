"""Company discovery engine – main orchestrator.

Pipeline:
    ConnectorManager.discover()  → list[ConnectorResult]
        ↓
    clean_companies()            → deduplicated list
        ↓
    validate_companies()         → validated list
        ↓
    return (results, metrics)

Never calls search providers directly. All discovery originates from
registered connectors via ConnectorManager.
"""

from __future__ import annotations

import logging

from app.connectors.connector_manager import ConnectorManager
from app.connectors.connector_result import ConnectorResult
from app.engines.discovery.company.company_cleaner import clean_companies
from app.engines.discovery.company.company_models import (
    CompanyDiscoveryResult,
    DiscoveryMetrics,
)
from app.engines.discovery.company.company_validator import validate_companies

logger = logging.getLogger(__name__)


class CompanyDiscoveryEngine:
    """Orchestrates the full company discovery pipeline.

    Flow::

        ConnectorManager.discover()   → list[ConnectorResult]
            ↓
        clean_companies()             → deduplicated list[CompanyDiscoveryResult]
            ↓
        validate_companies()          → validated list
            ↓
        return (results, metrics)
    """

    def __init__(self) -> None:
        """Initialize the discovery engine with a ConnectorManager."""
        self._manager = ConnectorManager()

    def discover(
        self,
        industry: str,
        location: str,
        *,
        limit: int = 100,
    ) -> tuple[list[CompanyDiscoveryResult], DiscoveryMetrics]:
        """Run the full discovery pipeline.

        Args:
            industry: e.g. ``"Construction Estimating"``.
            location: e.g. ``"Dallas Texas USA"``.
            limit: Maximum number of companies to return.

        Returns:
            Tuple of (companies, metrics).
        """
        logger.info(
            "Discovery started: industry=%r, location=%r, limit=%d",
            industry,
            location,
            limit,
        )

        # Step 1 — Discover via connected sources (no direct search provider calls).
        raw_connectors, conn_metadata = self._manager.discover(
            industry=industry,
            location=location,
            limit=limit,
        )
        logger.info(
            "Connector discovery complete: %d raw results",
            len(raw_connectors),
        )

        # Convert ConnectorResult → CompanyDiscoveryResult
        raw_companies: list[CompanyDiscoveryResult] = []
        for cr in raw_connectors:
            raw_companies.append(
                CompanyDiscoveryResult(
                    company_name=cr.company_name,
                    website=cr.website,
                    city=cr.city,
                    state=cr.state,
                    country=cr.country,
                    source=cr.source,  # type: ignore[arg-type]
                    confidence=cr.confidence,
                    source_url=cr.source_url,
                    discovery_reason=f"Discovered via {cr.source} connector",
                )
            )

        if not raw_companies:
            logger.info("No companies from connectors; returning empty")
            metrics = DiscoveryMetrics(total_found=0, total_validated=0, total_cleaned=0)
            return [], metrics

        # Step 2 — Deduplicate and normalize.
        cleaned, clean_metrics = clean_companies(raw_companies)
        logger.info("Cleaned: %d / %d passed", clean_metrics.total_cleaned, len(raw_companies))

        if not cleaned:
            logger.info("No companies after cleaning")
            metrics = DiscoveryMetrics(
                total_found=len(raw_companies),
                total_validated=0,
                total_cleaned=0,
                errors=clean_metrics.errors,
            )
            return [], metrics

        # Step 3 — Validate websites and names.
        validated, validate_metrics = validate_companies(cleaned)
        logger.info("Validated: %d / %d passed", validate_metrics.total_validated, len(cleaned))

        if not validated:
            logger.info("No companies passed validation")
            metrics = DiscoveryMetrics(
                total_found=len(raw_companies),
                total_validated=0,
                total_cleaned=clean_metrics.total_cleaned,
                errors=clean_metrics.errors + validate_metrics.errors,
            )
            return [], metrics

        # Aggregate metrics.
        final_metrics = DiscoveryMetrics(
            total_found=len(raw_companies),
            total_validated=validate_metrics.total_validated,
            total_cleaned=clean_metrics.total_cleaned,
            errors=clean_metrics.errors + validate_metrics.errors,
        )

        logger.info(
            "Discovery complete: %d companies | %d errors",
            len(validated),
            len(final_metrics.errors),
        )
        return validated, final_metrics
