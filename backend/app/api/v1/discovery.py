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
    return results
