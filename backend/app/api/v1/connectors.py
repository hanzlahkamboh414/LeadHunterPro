"""Texas Procurement Connector API routes.

Endpoint for discovering construction companies from Texas procurement
and contractor records. No search engines used — only industry-specific
public source data.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.connectors.connector_result import ConnectorResult
from app.connectors.texas_procurement import TexasProcurementConnector
from app.database.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connectors", tags=["Connectors"])

_connector = TexasProcurementConnector()


def _company_payload(c: ConnectorResult) -> dict[str, Any]:
    """Serialize one ConnectorResult for the Execute/discovery surface.

    Phase 3 Step 3: additive passthrough of the gate + AI intelligence
    namespaces already carried in ``c.metadata``. The response shape is
    unchanged — only ``verification``, ``ai`` and ``qualification`` (plus
    their flat gate aliases) are surfaced alongside the pre-existing keys,
    so deterministic verification stays authoritative and AI data survives to
    the final output. Records without AI data (rejected / unknown / bridge
    fixtures) emit empty ``ai``/``qualification`` dicts rather than inventing
    any.
    """
    return {
        "company_name": c.company_name,
        "website": c.website,
        "city": c.city,
        "state": c.state,
        "country": c.country,
        "source_url": c.source_url,
        "industry_focus": c.metadata.get("industry_focus", ""),
        "revenue_tier": c.metadata.get("revenue_tier", ""),
        "trade_category": c.metadata.get("trade_category", ""),
        "discovery_reason": c.metadata.get("discovery_reason", ""),
        # Phase 3 Step 3: gate + AI intelligence namespaces (additive).
        "verification_status": c.metadata.get("verification_status", ""),
        "verification_confidence": c.metadata.get("verification_confidence", 0.0),
        "gate_accepted": c.metadata.get("gate_accepted", False),
        "verification": c.metadata.get("verification", {}),
        "ai": c.metadata.get("ai", {}),
        "qualification": c.metadata.get("qualification", {}),
    }


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
    city: str | None = Query(
        None, min_length=1, max_length=100, description="City name"
    ),
    industry: str = Query("Construction Estimating", min_length=1, max_length=200),
    limit: int = Query(50, ge=1, le=200, description="Maximum results"),
    db: Session = Depends(get_db),
) -> Any:
    """Discover Texas construction companies from public procurement sources."""
    # Parse location from state+city for the connector
    location = f"{city} {state}" if city else state
    companies, metadata = _connector.search(
        industry=industry,
        location=location,
        limit=limit,
    )
    return {
        **metadata,
        "companies": [_company_payload(c) for c in companies],
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
    total_records = (
        len(_connector._companies) if hasattr(_connector, "_companies") else 0
    )
    live_results = getattr(_connector, "_live_results", None)
    return {
        "connector": _connector.connector_name,
        "priority": _connector.priority,
        "enabled": _connector.enabled,
        "health_check": _connector.health_check(),
        "total_records": total_records,
        "data_source": "fixture" if not live_results else "live",
        "bridge_mode": not bool(live_results),
        "note": "Bridge data - replace with live sources in Sprint 2.3",
    }
