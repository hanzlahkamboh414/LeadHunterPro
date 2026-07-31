"""Crawler API routes."""

import logging

from fastapi import APIRouter, Query

from app.services.crawler_service import CrawlerService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/crawler", tags=["Crawler"])

service = CrawlerService()


@router.get("/")
def crawl(
    website: str = Query(
        ...,
        min_length=1,
        description="Website URL to crawl",
    ),
) -> dict:
    """Crawl a website and extract structured data."""
    return service.crawl(website)
