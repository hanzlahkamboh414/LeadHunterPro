"""Texas Procurement Connector API routes.

Endpoint for discovering construction companies from Texas procurement
and contractor records. No search engines used — only industry-specific
public source data.
"""

import logging
from typing import Any, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.engines.source_connectors.texas_procurement import TexasProcurementConnector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connectors", tags=["Connectors"])

_connector = TexasProcurementConnector()


@router.get(
    "/texas-procurement",
    summary="Discover Texas construction companies",
    description=(
        "Returns construction companies from Texas procurement and "
        "contractor records. No Google/Bing/DuckDuckGo search is used."
    ),
)
def discover_texas_procurement(
    state: str = Query("TX", min_length=2, max_length=2, description="US state code"),
    city: str | None = Query(None, min_length=1, max_length=100, description="City name"),
    industry: str = Query("Construction Estimating", min_length=1, max_length=200),
    limit: int = Query(50, ge=1, le=200, description="Maximum results"),
    db: Session = Depends(get_db),
) -> Any:
    """Discover Texas construction companies from public procurement sources."""
    companies, metadata = _connector.discover(
        state=state,
        city=city,
        industry=industry,
        limit=limit,
    )
    return {
        **metadata,
        "companies": [
            {
                "company_name": c["company_name"],
                "website": c["website"],
                "city": c["city"],
                "state": c["state"],
                "country": c["country"],
                "source_url": c["source_url"],
                "industry_focus": c.get("industry_focus", ""),
                "revenue_tier": c.get("revenue_tier", ""),
            }
            for c in companies
        ],
    }


@router.get(
    "/texas-procurement/summary",
    summary="Get Texas procurement connector info",
    description="Returns metadata about the Texas procurement connector.",
)
def get_texas_procurement_summary(
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return connector metadata and capability summary."""
    return {
        "connector": _connector.name,
        "description": _connector.description,
        "available": _connector.is_available(),
        "total_records": len(_connector._TAXAS_CONSTRUCTION_COMPANIES if hasattr(_connector, '_TAXAS_CONSTRUCTION_COMPANIES') else []),
        "supported_states": ["TX"],
        "supported_cities": ["Dallas", "Houston", "Austin", "San Antonio", "Arlington", "Fort Worth"],
    }
