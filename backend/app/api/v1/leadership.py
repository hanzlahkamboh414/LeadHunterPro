"""Leadership discovery API routes."""

import logging

from fastapi import APIRouter, Query

from app.services.leadership_service import LeadershipService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/leadership", tags=["Leadership"])

service = LeadershipService()


@router.get("/")
def discover(
    website: str = Query(
        ...,
        min_length=1,
        description="Company website to discover leadership contacts from",
    ),
) -> list[dict]:
    """Discover leadership contacts from a company website."""
    return service.discover(website)
