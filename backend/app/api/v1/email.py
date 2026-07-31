"""Email discovery API routes."""

import logging

from fastapi import APIRouter, Query

from app.services.email_service import EmailService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email", tags=["Email Discovery"])

service = EmailService()


@router.get("/")
def discover(
    website: str = Query(
        ...,
        min_length=1,
        description="Company website URL to discover emails from",
    ),
) -> dict:
    """Discover email addresses from a company website."""
    return service.discover(website)
