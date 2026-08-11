"""Company discovery API routes."""

import logging
from typing import Any, List, Union

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.engines.discovery.company.company_discovery_engine import CompanyDiscoveryEngine
from app.engines.discovery.company.company_models import CompanyDiscoveryResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discovery", tags=["Discovery"])

engine = CompanyDiscoveryEngine()


def _company_payload(c: CompanyDiscoveryResult) -> dict[str, Any]:
    """Serialize one CompanyDiscoveryResult for the discovery surface.

    Phase 3 Step 4: additive passthrough of the gate + AI intelligence
    namespaces already carried in ``result.metadata`` (attached by the
    connector in Phase 3 Step 2 and carried verbatim through the engine,
    cleaner and validator). Curation keeps internal provenance keys out of the
    response. Records without AI data (rejected / unknown / bridge fixtures)
    emit empty ``ai`` / ``qualification`` dicts — never invented.
    """
    return {
        "company_name": c.company_name,
        "website": c.website,
        "city": c.city,
        "state": c.state,
        "country": c.country,
        "source": c.source,
        "confidence": c.confidence,
        "source_url": c.source_url,
        "discovery_reason": c.discovery_reason,
        # Phase 3 Step 4: gate + AI intelligence namespaces (additive).
        "verification_status": c.metadata.get("verification_status", ""),
        "verification_confidence": c.metadata.get("verification_confidence", 0.0),
        "gate_accepted": c.metadata.get("gate_accepted", False),
        "verification": c.metadata.get("verification", {}),
        "ai": c.metadata.get("ai", {}),
        "qualification": c.metadata.get("qualification", {}),
    }


@router.get(
    "/companies",
    summary="Discover construction companies",
    description=(
        "Search public sources for companies matching the given industry "
        "and location. Returns validated, deduplicated results."
        "If no providers are available or all return CAPTCHA-blocked pages, "
        "a structured diagnostic is returned explaining the failure."
    ),
    responses={
        200: {
            "description": "Successful discovery — either a list of companies or a diagnostic object.",
            "model": Any,
        },
    },
)
def discover_companies(
    industry: str = Query(
        ...,
        min_length=1,
        max_length=200,
        description="Industry to search for, e.g. 'Construction Estimating'",
    ),
    location: str = Query(
        ...,
        min_length=1,
        max_length=200,
        description="Geographic location, e.g. 'Dallas Texas USA'",
    ),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of results"),
    db: Session = Depends(get_db),
) -> Any:
    """Discover companies from public web sources."""
    results, metrics = engine.discover(
        industry=industry, location=location, limit=limit
    )
    return [_company_payload(c) for c in results]
