"""Company analysis engine – enriches company profiles."""

import logging

from app.crawler.website_crawler import WebsiteCrawler

logger = logging.getLogger(__name__)


class CompanyEngine:
    """Enrich company data by crawling its website."""

    def __init__(self) -> None:
        self._crawler = WebsiteCrawler()

    def enrich(self, website: str) -> dict:
        """Crawl a company website and return structured data.

        Args:
            website: The company's base URL.

        Returns:
            Dictionary with title, description, emails, phones, links.
        """
        return self._crawler.crawl(website)
