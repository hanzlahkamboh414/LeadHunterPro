"""Company discovery API routes."""

import logging
from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.engines.discovery.company.company_discovery_engine import CompanyDiscoveryEngine
from app.engines.discovery.company.company_models import CompanyDiscoveryResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discovery", tags=["Discovery"])

engine = CompanyDiscoveryEngine()


@router.get(
    "/companies",
    response_model=List[CompanyDiscoveryResult],
    summary="Discover construction companies",
    description=(
        "Search public sources for companies matching the given industry "
        "and location. Returns validated, deduplicated results."
    ),
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
) -> List[CompanyDiscoveryResult]:
    """Discover companies from public web sources."""
    results, _ = engine.discover(industry=industry, location=location, limit=limit)
    return results
