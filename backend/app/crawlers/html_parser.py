"""HTML parsing utilities for web content extraction.

Provides BeautifulSoup-based extraction of common webpage elements
(titles, descriptions, emails, phones, social links). This parser
is generic -- it does not know about companies or connectors.
It simply extracts structured data from raw HTML.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from app.email.email_cleaner import EMAIL_CLEAN_PATTERN, is_acceptable_email

logger = logging.getLogger(__name__)

# Email regex pattern — the SHARED grammar (dot-atom local part), so a
# trailing-dot local like ``own.@jose.elcook`` is not even harvested.
_EMAIL_PATTERN = EMAIL_CLEAN_PATTERN

# Phone regex pattern (international format)
_PHONE_PATTERN = re.compile(r"\+?[\d\s().-]{7,}\d")

# Social media domain patterns
_SOCIAL_DOMAINS: dict[str, str] = {
    "linkedin": "linkedin.com",
    "facebook": "facebook.com",
    "twitter": "twitter.com",
    "x": "x.com",
    "instagram": "instagram.com",
}


@dataclass
class ParsedPage:
    """Structured extraction result from an HTML page.

    Attributes:
        url: The page URL that was parsed.
        title: Page title text.
        description: Meta description content.
        emails: List of unique email addresses found.
        phones: List of unique phone numbers found.
        social_links: Dictionary mapping platform names to URLs.
        text_content: Normalized plain text content.
        links: List of absolute URLs found on the page.
        h1_texts: List of H1 heading texts.
        meta_keywords: Meta keywords content if present.
    """

    url: str
    title: str = ""
    description: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    social_links: dict[str, str] = field(default_factory=dict)
    text_content: str = ""
    links: list[str] = field(default_factory=list)
    h1_texts: list[str] = field(default_factory=list)
    meta_keywords: str = ""

    def is_empty(self) -> bool:
        """Check if any meaningful data was extracted."""
        return not any(
            [
                self.title,
                self.description,
                self.emails,
                self.phones,
                self.social_links,
                self.links,
                self.h1_texts,
            ]
        )


class HTMLParser:
    """BeautifulSoup-based HTML extraction utility.

    Extracts structured data from raw HTML without any business
    logic. Each method is independent and can be used separately.
    """

    def __init__(self, parser_engine: str = "html.parser") -> None:
        """Initialize the HTML parser.

        Args:
            parser_engine: BeautifulSoup parser engine ('html.parser',
                'lxml', 'html5lib'). Defaults to Python's built-in parser.
        """
        self._parser_engine = parser_engine
        self._soup_class: Any = None
        self._import_soup()

    def _import_soup(self) -> None:
        """Import BeautifulSoup, falling back gracefully."""
        try:
            from bs4 import BeautifulSoup

            self._soup_class = BeautifulSoup
        except ImportError:
            logger.warning("BeautifulSoup not available, HTML parsing disabled")
            self._soup_class = None

    def parse(self, html: str, base_url: str = "") -> ParsedPage:
        """Parse HTML and extract structured data.

        Args:
            html: Raw HTML content string.
            base_url: Base URL for resolving relative links.

        Returns:
            ParsedPage with extracted data.

        Raises:
            ValueError: If HTML is empty or None.
        """
        if not html:
            raise ValueError("HTML content cannot be empty")

        if self._soup_class is None:
            logger.error("BeautifulSoup not available, returning empty parse result")
            return ParsedPage(url=base_url)

        soup = self._soup_class(html, self._parser_engine)
        return self._extract(soup, base_url)

    def _extract(self, soup: Any, base_url: str) -> ParsedPage:
        """Extract data from a BeautifulSoup object.

        Args:
            soup: Parsed BeautifulSoup object.
            base_url: Base URL for link resolution.

        Returns:
            ParsedPage with extracted data.
        """
        page = ParsedPage(url=base_url)

        # Title
        title_tag = soup.find("title")
        if title_tag:
            page.title = title_tag.get_text(strip=True)

        # Meta description
        desc_meta = soup.find("meta", attrs={"name": "description"})
        if desc_meta:
            page.description = desc_meta.get("content", "")

        # Meta keywords
        kw_meta = soup.find("meta", attrs={"name": "keywords"})
        if kw_meta:
            page.meta_keywords = kw_meta.get("content", "")

        # H1 tags
        h1_tags = soup.find_all("h1")
        page.h1_texts = [
            h1.get_text(strip=True) for h1 in h1_tags if h1.get_text(strip=True)
        ]

        # Emails
        page.emails = sorted(set(self._find_emails(soup)))

        # Phones
        page.phones = sorted(set(self._find_phones(soup)))

        # Social links
        page.social_links = self._find_social_links(soup)

        # All links
        page.links = sorted(set(self._find_links(soup, base_url)))

        # Text content
        page.text_content = self._normalize_text(soup.get_text(" ", strip=True))

        return page

    def _find_emails(self, soup: Any) -> list[str]:
        """Extract email addresses from HTML.

        Args:
            soup: BeautifulSoup object.

        Returns:
            List of unique email addresses.
        """
        emails: set[str] = set()

        # From text content.
        #
        # The separator is the whole point, not a detail: a contact block
        # puts the phone and the email in adjacent inline tags, and an
        # unseparated ``get_text()`` welds them into
        # ``620-6727email.office@premierelectricalcontracting.com`` — a
        # string that is grammatically a valid address and therefore cannot
        # be refused downstream by syntax. The same idiom (a space join) is
        # already what ``text_content`` uses a few lines above.
        text = soup.get_text(" ")
        for hit in _EMAIL_PATTERN.findall(text):
            # Page source carries machine strings shaped like addresses
            # (Sentry DSNs, mailing-list ids, example.com placeholders) and
            # strings that are not addresses at all (``own.@jose.elcook``) —
            # the shared gate keeps both out of lead data.
            if is_acceptable_email(hit):
                emails.add(hit)

        # From mailto: links
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.startswith("mailto:"):
                email = href[7:].split("?")[0].strip().lower()
                if is_acceptable_email(email):
                    emails.add(email)

        return sorted(emails)

    def _find_phones(self, soup: Any) -> list[str]:
        """Extract phone numbers from HTML.

        Args:
            soup: BeautifulSoup object.

        Returns:
            List of unique phone numbers.
        """
        # Separated for the same reason as _find_emails: welded text runs
        # the previous element's trailing digits onto the number
        # ("info2" + "620-6727" -> "2620-6727"), which is a wrong phone that
        # still looks like a phone. The pattern already tolerates spaces, so
        # the join costs nothing.
        text = soup.get_text(" ")
        phones = _PHONE_PATTERN.findall(text)

        # Clean up phone numbers
        cleaned: set[str] = set()
        for phone in phones:
            # Remove extra whitespace, parentheses, plus signs, dashes, dots
            clean = re.sub(r"[\s()+\-\.]", "", phone)
            # Keep only valid-looking numbers (must be all digits now)
            if len(clean) >= 7 and clean.isdigit():
                cleaned.add(phone)

        return sorted(cleaned)

    def _find_social_links(self, soup: Any) -> dict[str, str]:
        """Extract social media profile links.

        Args:
            soup: BeautifulSoup object.

        Returns:
            Dictionary mapping platform names to URLs.
        """
        links: dict[str, str] = {}

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].lower()
            for platform, domain in _SOCIAL_DOMAINS.items():
                if domain in href and platform not in links:
                    links[platform] = a_tag["href"]
                    break

        return links

    def _find_links(self, soup: Any, base_url: str) -> list[str]:
        """Extract all absolute URLs from the page.

        Args:
            soup: BeautifulSoup object.
            base_url: Base URL for resolving relative paths.

        Returns:
            List of unique absolute URLs.
        """
        urls: set[str] = set()

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            # Skip anchor links and javascript
            if href.startswith(("#", "javascript:")):
                continue
            # Resolve relative URLs
            absolute = urljoin(base_url, href)
            urls.add(absolute)

        return sorted(urls)

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize whitespace in text content.

        Args:
            text: Raw text with potential extra whitespace.

        Returns:
            Text with collapsed whitespace.
        """
        # Collapse multiple whitespace characters into single space
        return re.sub(r"\s+", " ", text).strip()

    def extract_title(self, html: str, base_url: str = "") -> str:
        """Extract just the page title.

        Args:
            html: Raw HTML content.
            base_url: Base URL (unused but kept for API consistency).

        Returns:
            Page title string.
        """
        result = self.parse(html, base_url)
        return result.title

    def extract_emails(self, html: str, base_url: str = "") -> list[str]:
        """Extract just the email addresses.

        Args:
            html: Raw HTML content.
            base_url: Base URL (unused but kept for API consistency).

        Returns:
            List of email addresses.
        """
        result = self.parse(html, base_url)
        return result.emails

    def extract_phones(self, html: str, base_url: str = "") -> list[str]:
        """Extract just the phone numbers.

        Args:
            html: Raw HTML content.
            base_url: Base URL (unused but kept for API consistency).

        Returns:
            List of phone numbers.
        """
        result = self.parse(html, base_url)
        return result.phones

    def extract_meta(self, html: str, base_url: str = "") -> dict[str, str]:
        """Extract metadata (title, description, keywords).

        Args:
            html: Raw HTML content.
            base_url: Base URL (unused but kept for API consistency).

        Returns:
            Dictionary of metadata fields.
        """
        result = self.parse(html, base_url)
        return {
            "title": result.title,
            "description": result.description,
            "keywords": result.meta_keywords,
        }
