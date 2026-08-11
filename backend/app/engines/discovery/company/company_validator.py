"""Website and company-name validation for discovered leads.

Checks that each discovered company's website resolves to a live HTTP
server and that the company name is non-empty and plausible.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

from app.engines.discovery.company.company_models import (
    CompanyDiscoveryResult,
    DiscoveryMetrics,
)

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
}

# Domains that are clearly not commercial company sites.
_BLOCKED_DOMAIN_PATTERNS = re.compile(
    r"(wikipedia\.org|\.gov$|\.edu$|linkedin\.com/in/|github\.com/|"
    r"facebook\.com/|twitter\.com/|instagram\.com/| indeed\.com|monster\.com)",
    re.IGNORECASE,
)


def validate_companies(
    companies: list[CompanyDiscoveryResult],
    *,
    timeout: int = 10,
) -> tuple[list[CompanyDiscoveryResult], DiscoveryMetrics]:
    """Validate a list of discovered companies.

    Args:
        companies: Raw discovery results to validate.
        timeout: HTTP HEAD request timeout in seconds.

    Returns:
        Tuple of (valid_companies, metrics).
    """
    metrics = DiscoveryMetrics(total_found=len(companies))
    valid: list[CompanyDiscoveryResult] = []

    for company in companies:
        if not _is_valid_name(company.company_name):
            metrics.errors.append(f"Invalid name: {company.company_name!r}")
            continue
        if not _is_live_website(company.normalized_website, timeout=timeout):
            metrics.errors.append(f"Dead website: {company.normalized_website}")
            continue
        if _is_blocked_domain(company.normalized_website):
            metrics.errors.append(f"Blocked domain: {company.normalized_website}")
            continue
        # Boost confidence when website is confirmed live.
        company = CompanyDiscoveryResult(
            company_name=company.company_name,
            website=company.website,
            city=company.city,
            state=company.state,
            country=company.country,
            source=company.source,
            confidence=min(company.confidence + 0.15, 1.0),
            source_url=company.source_url,
            discovery_reason=company.discovery_reason,
            # Phase 3 Step 4: verification + AI metadata survives validation
            # unchanged — only confidence is boosted here.
            metadata=dict(company.metadata),
        )
        valid.append(company)
        metrics.total_validated += 1

    logger.info(
        "Validation: %d / %d companies passed",
        metrics.total_validated,
        metrics.total_found,
    )
    return valid, metrics


def _is_valid_name(name: str) -> bool:
    """Return True if the company name is non-empty and looks real."""
    if not name or len(name.strip()) < 3:
        return False
    # Must contain at least one alphabetic character.
    return bool(re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", name))


def _is_live_website(url: str, *, timeout: int = 10) -> bool:
    """Return True if the URL responds with a 2xx status."""
    try:
        resp = requests.head(
            url,
            headers=_HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
        return 200 <= resp.status_code < 300
    except requests.RequestException:
        return False


def _is_blocked_domain(url: str) -> bool:
    """Return True if the URL points to a non-commercial domain."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    return bool(_BLOCKED_DOMAIN_PATTERNS.search(domain))
