"""Social media link discovery engine."""

import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SOCIAL_DOMAINS = {
    "linkedin": "linkedin.com",
    "facebook": "facebook.com",
    "twitter": ("twitter.com", "x.com"),
    "instagram": "instagram.com",
}


def extract_social_links(html: str, base_url: str) -> dict[str, list[str]]:
    """Extract social media profile links from HTML.

    Args:
        html: Raw HTML content.
        base_url: Base URL for resolving relative links.

    Returns:
        Dictionary mapping platform name to list of URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: dict[str, list[str]] = {k: [] for k in SOCIAL_DOMAINS}

    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"])
        lower = href.lower()
        for platform, domain in SOCIAL_DOMAINS.items():
            if isinstance(domain, tuple):
                if any(d in lower for d in domain):
                    results[platform].append(href)
            elif domain in lower:
                results[platform].append(href)

    return {k: sorted(set(v)) for k, v in results.items()}
