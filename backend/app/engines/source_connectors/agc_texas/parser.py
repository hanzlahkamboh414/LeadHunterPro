"""AGC Texas data parser.

Parses raw HTML or JSON responses from AGC Texas member directory
into standardized company dictionaries.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class AgcTexasParser:
    """Parse AGC Texas member directory data.

    This parser handles both HTML scraping and JSON API responses
    from the AGC Texas website.
    """

    # Patterns for extracting company information
    COMPANY_NAME_PATTERN = re.compile(r"<h[1-6][^>]*>([^<]+)</h[1-6]>")
    WEBSITE_PATTERN = re.compile(r"https?://[^\s<\"']+\.(?:com|org|net)[^\s<\"']*")
    CITY_PATTERN = re.compile(r",\s*([A-Z][a-zA-Z\s]+),\s*TX\b")
    PHONE_PATTERN = re.compile(r"([\+]?[\d\s\-\(\)]{7,})")

    def parse_member_list(self, html: str) -> list[dict[str, Any]]:
        """Parse HTML member directory listing.

        Args:
            html: Raw HTML content from AGC Texas member directory.

        Returns:
            List of parsed company data dictionaries.
        """
        companies: list[dict[str, Any]] = []

        try:
            # Try to extract structured data from HTML
            # Note: Real implementation would need to match actual AGC site structure
            soup_matches = self._extract_from_html(html)
            companies.extend(soup_matches)

            # Fallback: regex-based extraction
            if not companies:
                companies.extend(self._extract_with_regex(html))

        except Exception as exc:
            logger.error("Failed to parse AGC Texas HTML: %s", exc, exc_info=True)

        return companies

    def parse_member_detail(self, html: str) -> dict[str, Any]:
        """Parse individual member profile page.

        Args:
            html: Raw HTML content from member detail page.

        Returns:
            Dictionary with member details.
        """
        company: dict[str, Any] = {}

        try:
            # Extract company name
            name_match = self.COMPANY_NAME_PATTERN.search(html)
            if name_match:
                company["company_name"] = name_match.group(1).strip()

            # Extract website
            website_match = self.WEBSITE_PATTERN.search(html)
            if website_match:
                company["website"] = website_match.group(1)

            # Extract city
            city_match = self.CITY_PATTERN.search(html)
            if city_match:
                company["city"] = city_match.group(1).strip()
                company["state"] = "TX"
                company["country"] = "USA"

            # Extract phone
            phone_match = self.PHONE_PATTERN.search(html)
            if phone_match:
                company["phone"] = phone_match.group(1).strip()

        except Exception as exc:
            logger.error("Failed to parse member detail: %s", exc, exc_info=True)

        return company

    def _extract_from_html(self, html: str) -> list[dict[str, Any]]:
        """Try to extract company data using BeautifulSoup-like parsing."""
        from bs4 import BeautifulSoup  # Lazy import

        companies: list[dict[str, Any]] = []
        soup = BeautifulSoup(html, "html.parser")

        # Look for member cards/entries
        for card in soup.select(".member-card, .directory-item, .company-entry, article"):
            company: dict[str, Any] = {}

            # Extract name
            name_el = card.select_one("h1, h2, h3, .company-name, .name")
            if name_el:
                company["company_name"] = name_el.get_text(strip=True)

            # Extract website
            link_el = card.select_one("a[href*='http']")
            if link_el:
                href = link_el.get("href", "")
                if href.startswith("//"):
                    href = "https:" + href
                company["website"] = href

            # Extract location
            loc_el = card.select_one(".location, .city-state, .address")
            if loc_el:
                text = loc_el.get_text()
                city_match = self.CITY_PATTERN.search(text)
                if city_match:
                    company["city"] = city_match.group(1)
                    company["state"] = "TX"
                    company["country"] = "USA"

            if company.get("company_name"):
                companies.append(company)

        return companies

    def _extract_with_regex(self, html: str) -> list[dict[str, Any]]:
        """Fallback regex-based extraction."""
        companies: list[dict[str, Any]] = []

        # Try to find patterns like "Company Name - City, TX"
        pattern = re.compile(r"([A-Z][a-zA-Z\s&\-]+)\s*[–-]\s*([A-Za-z\s]+),\s*TX\b")
        for match in pattern.finditer(html):
            company: dict[str, Any] = {
                "company_name": match.group(1).strip(),
                "city": match.group(2).strip(),
                "state": "TX",
                "country": "USA",
            }
            companies.append(company)

        return companies

    @staticmethod
    def is_valid_company(data: dict[str, Any]) -> bool:
        """Check if parsed data represents a valid construction company."""
        required_fields = ["company_name", "city", "state"]
        if not all(field in data and data[field] for field in required_fields):
            return False

        # Must be Texas-based
        if data.get("state", "").upper() != "TX":
            return False

        # Company name should not be empty or suspicious
        name = data.get("company_name", "").lower()
        skip_keywords = ["login", "register", "contact us", "about us"]
        if any(kw in name for kw in skip_keywords):
            return False

        return True
