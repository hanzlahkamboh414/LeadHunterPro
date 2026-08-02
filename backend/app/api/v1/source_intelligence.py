"""Source Intelligence API routes."""

import logging
from typing import Any, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.engines.source_intelligence.source_planner import SourcePlanner
from app.engines.source_intelligence.source_models import (
    SourcePlannerRequest,
    SourceRecord,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/source-intelligence", tags=["Source Intelligence"])

_planner = SourcePlanner()


@router.get(
    "/sources",
    summary="Plan construction industry sources",
    description=(
        "Returns a ranked list of public sources most relevant for "
        "discovering construction-estimating companies in the given "
        "industry and location. No crawling is performed."
    ),
)
def plan_sources(
    industry: str = Query(
        ...,
        min_length=1,
        max_length=200,
        description="Industry, e.g. 'Construction Estimating'",
    ),
    country: str = Query("USA", min_length=1, max_length=100, description="Country"),
    state: str | None = Query(None, min_length=1, max_length=100, description="US state"),
    city: str | None = Query(None, min_length=1, max_length=100, description="City name"),
    db: Session = Depends(get_db),
) -> Any:
    """Plan and rank public sources for construction lead discovery."""
    request = SourcePlannerRequest(
        industry=industry.strip(),
        country=country.strip(),
        state=state.strip() if state else None,
        city=city.strip() if city else None,
    )
    result = _planner.plan(request)

    # Build serializable response.
    sources_out: list[dict[str, Any]] = [
        {
            "name": s.name,
            "type": s.source_type,
            "country": s.country,
            "state": s.state,
            "city": s.city,
            "priority": s.priority,
            "crawl_strategy": s.crawl_strategy,
            "supports_company_discovery": s.supports_company_discovery,
            "supports_bid_discovery": s.supports_bid_discovery,
            "supports_leadership": s.supports_leadership,
            "supports_contact": s.supports_contact,
            "url": s.url,
            "notes": s.notes,
        }
        for s in result.sources
    ]
    return {
        "query_summary": result.query_summary,
        "total_sources": result.total_sources,
        "skipped_sources": result.skipped_sources,
        "sources": sources_out,
    }
