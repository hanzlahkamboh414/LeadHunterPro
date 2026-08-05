"""Deduplicator for connector-discovered companies.

Removes duplicate companies based on normalized domain or company name.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from app.engines.source_connectors.normalizer import Normalizer
from app.engines.source_connectors.sdk import CompanyResult

logger = logging.getLogger(__name__)


class Deduplicator:
    """Deduplicates a list of :class:`CompanyResult` instances.

    Deduplication is performed using two strategies:
    1. **Domain-level** — normalise the URL to its registered domain and
       compare. Two companies with the same domain are considered duplicates.
    2. **Name-level** — strip corporate suffixes and compare the resulting
       normalised names (case-insensitive).

    Domain-level dedup takes priority; if two records share a domain,
    the first one encountered is kept.
    """

    def __init__(self, *, keep_first: bool = True) -> None:
        """Initialize the deduplicator.

        Args:
            keep_first: If True, keep the first occurrence of each
                duplicate. If False, keep the last.
        """
        self._keep_first = keep_first

    def deduplicate(self, results: list[CompanyResult]) -> list[CompanyResult]:
        """Remove duplicate entries from *results*.

        Args:
            results: List of company results to deduplicate.

        Returns:
            A new list with duplicates removed.
        """
        if len(results) <= 1:
            return list(results)

        seen_domains: set[str] = set()
        seen_names: set[str] = set()
        kept: list[CompanyResult] = []

        iterator = reversed(results) if not self._keep_first else results

        for company in iterator:
            domain_key = self._extract_domain_key(company.website)
            name_key = Normalizer.normalised_name_key(company.company_name)

            domain_dup = domain_key in seen_domains
            name_dup = name_key in seen_names

            if domain_dup or name_dup:
                # Duplicate by either domain or normalized name — skip.
                # Rationale: same-domain OR same-name suggests the same entity.
                logger.debug("Dropping duplicate: %s (%s)", company.company_name, domain_key)
                continue

            if not domain_dup:
                seen_domains.add(domain_key)
            if not name_dup:
                seen_names.add(name_key)

            kept.append(company)

        logger.info(
            "Deduplicated: %d → %d (removed %d)",
            len(results),
            len(kept),
            len(results) - len(kept),
        )
        return kept

    @staticmethod
    def _extract_domain_key(url: str) -> str:
        """Extract a normalised domain key from a URL.

        Handles URLs with and without scheme, and normalises to lowercase.

        Args:
            url: Company website URL.

        Returns:
            Normalised domain string (e.g. ``"acme.com"``).
        """
        if not url:
            return ""
        # Ensure scheme for urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        # Strip www. prefix for cleaner keys
        hostname = re.sub(r"^www\.", "", hostname, flags=re.IGNORECASE)
        return hostname.lower().strip()

    @staticmethod
    def count_duplicates(results: list[CompanyResult]) -> int:
        """Count how many duplicates would be removed from *results*.

        Args:
            results: List of company results.

        Returns:
            Number of duplicate entries.
        """
        return len(results) - len(Deduplicator().deduplicate(results))
