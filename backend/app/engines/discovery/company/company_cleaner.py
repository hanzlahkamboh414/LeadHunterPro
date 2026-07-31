"""Deduplication and normalization for discovered company candidates.

Removes duplicate entries by domain and by normalized company name, then
normalizes LLC/Inc suffixes for consistent comparison.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from app.engines.discovery.company.company_models import (
    CompanyDiscoveryResult,
    DiscoveryMetrics,
)

logger = logging.getLogger(__name__)

# Common corporate suffixes to normalize away during dedup comparison.
_SUFFIX_RE = re.compile(
    r"\s*(Inc\.?|Incorporated|LLC|L\.L\.C\.?|Ltd\.?|Limited|Corp\.?|"
    r"Corporation|Co\.?|Company|Group|Holdings)\s*$",
    re.IGNORECASE,
)


def clean_companies(
    companies: list[CompanyDiscoveryResult],
) -> tuple[list[CompanyDiscoveryResult], DiscoveryMetrics]:
    """Remove duplicates and normalize company names.

    Deduplication keys (lower-cased, suffix-stripped):
    - Website domain
    - Company name

    Args:
        companies: Validated company candidates.

    Returns:
        Tuple of (clean_companies, metrics).
    """
    metrics = DiscoveryMetrics(total_found=len(companies))
    seen_domains: set[str] = set()
    seen_names: set[str] = set()
    cleaned: list[CompanyDiscoveryResult] = []

    for company in companies:
        domain = _extract_domain(company.normalized_website)
        norm_name = _normalize_name(company.company_name)

        dup_domain = domain in seen_domains
        dup_name = norm_name in seen_names
        seen_domains.add(domain)
        seen_names.add(norm_name)

        if dup_domain or dup_name:
            metrics.errors.append(f"Duplicate removed: {company.company_name} ({domain})")
            continue

        # Normalize the stored name (strip suffixes for display consistency).
        normalized = CompanyDiscoveryResult(
            company_name=_normalize_name(company.company_name),
            website=company.website,
            city=company.city,
            state=company.state,
            country=company.country,
            source=company.source,
            confidence=company.confidence,
        )
        cleaned.append(normalized)
        metrics.total_cleaned += 1

    logger.info(
        "Cleaning: %d kept, %d removed from %d input",
        metrics.total_cleaned,
        metrics.total_found - metrics.total_cleaned,
        metrics.total_found,
    )
    return cleaned, metrics


def _extract_domain(url: str) -> str:
    """Return the normalized domain string from a URL."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host.lower().replace("www.", "")


def _normalize_name(name: str) -> str:
    """Lower-case and strip corporate suffixes for comparison."""
    name = _SUFFIX_RE.sub("", name).strip()
    return name.lower()
