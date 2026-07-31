"""Crawler service – high-level crawling orchestration."""

import logging

from app.crawler.website_crawler import WebsiteCrawler

logger = logging.getLogger(__name__)


class CrawlerService:
    """Coordinate website crawling operations."""

    def __init__(self) -> None:
        self._crawler = WebsiteCrawler()

    def crawl(self, website: str) -> dict:
        """Crawl a website and return extracted data.

        Args:
            website: The base URL to crawl.

        Returns:
            Dictionary with title, description, emails, phones, links.
        """
        return self._crawler.crawl(website)
