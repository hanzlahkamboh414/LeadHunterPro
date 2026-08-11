"""Website scraping engine – fetches and parses web pages."""

import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.email.email_cleaner import EMAIL_CLEAN_PATTERN, clean_emails

logger = logging.getLogger(__name__)


class WebsiteEngine:
    """Fetch and parse company website content."""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        )
    }

    def fetch(self, url: str) -> str:
        """Fetch a URL and return raw HTML.

        Args:
            url: The page to fetch.

        Returns:
            Raw HTML response text.

        Raises:
            requests.HTTPError: If the response status is >= 400.
        """
        response = requests.get(url, headers=self.HEADERS, timeout=30)
        response.raise_for_status()
        return response.text

    def parse(self, html: str, base_url: str) -> dict:
        """Parse HTML and extract structured company data.

        Args:
            html: Raw HTML content.
            base_url: Base URL for resolving relative links.

        Returns:
            Dictionary with title, description, emails, phones, and social links.
        """
        soup = BeautifulSoup(html, "html.parser")

        title = soup.title.get_text(strip=True) if soup.title else ""
        meta_desc = soup.find("meta", attrs={"name": "description"})
        description = meta_desc.get("content", "") if meta_desc else ""
        text = soup.get_text(" ")

        emails = clean_emails(EMAIL_CLEAN_PATTERN.findall(text))

        phones_raw = re.findall(r"\+?\d[\d\s().-]{7,}\d", text)
        phones = sorted(set(phones_raw))

        links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            links.append(href)

        contact_page = next(
            (l for l in links if "contact" in l.lower()), ""
        )
        about_page = next(
            (l for l in links if "about" in l.lower()), ""
        )

        linkedin = sorted(set(l for l in links if "linkedin.com" in l.lower()))
        facebook = sorted(set(l for l in links if "facebook.com" in l.lower()))
        twitter = sorted(set(l for l in links if "twitter.com" in l.lower() or "x.com" in l.lower()))
        instagram = sorted(set(l for l in links if "instagram.com" in l.lower()))

        return {
            "title": title,
            "description": description,
            "emails": emails,
            "phones": phones,
            "linkedin": linkedin,
            "facebook": facebook,
            "twitter": twitter,
            "instagram": instagram,
            "contact_page": contact_page,
            "about_page": about_page,
        }
