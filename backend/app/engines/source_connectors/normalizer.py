"""Normalizer for connector-discovered company data.

Standardises company name casing, strips common corporate suffixes,
and validates that required fields are present before a record is
accepted into downstream pipelines.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.engines.source_connectors.sdk import CompanyResult

logger = logging.getLogger(__name__)

# Common corporate suffixes to strip when deduplicating by name.
# Longer/more specific suffixes first to avoid premature matches.
_CORPORATE_SUFFIXES = (
    "Corporation",
    "Limited",
    "Inc.",
    "Ltd.",
    "GmbH",
    "AG",
    "PLC",
    "NV",
    "SA",
    "Company",
    "Inc",
    "Ltd",
    "LLC",
    "L.L.C.",
    "Corp.",
    "Corp",
    "Co.",
    "Co",
)

# Suffixes that match as standalone words but shouldn't strip other words
_SAFE_SUFFIXES = {"Inc", "Ltd", "Co", "Corp"}


class Normalizer:
    """Normalises raw company dicts into :class:`CompanyResult` instances.

    Normalisation steps:
    1. Strip leading/trailing whitespace from all string fields.
    2. Title-case the company name.
    3. Remove known corporate suffixes from the name (for dedup purposes).
    4. Validate that required fields are non-empty.
    5. Default missing optional fields to sensible values.
    """

    def __init__(self, strip_suffixes: bool = True) -> None:
        """Initialize the normalizer.

        Args:
            strip_suffixes: If True, strip corporate suffixes from names
                during normalization.
        """
        self._strip_suffixes = strip_suffixes

    def normalize(self, data: dict[str, Any]) -> CompanyResult | None:
        """Normalize a single raw company dictionary.

        Args:
            data: Raw company data from a connector.

        Returns:
            A validated :class:`CompanyResult`, or ``None`` if the
            record fails validation (e.g. missing website).
        """
        try:
            name = str(data.get("company_name", "")).strip()
            website = str(data.get("website", "")).strip()
            city = str(data.get("city", "")).strip()
            state = str(data.get("state", "")).strip()
            country = str(data.get("country", "USA")).strip() or "USA"

            if not name:
                logger.debug("Skipping record: empty company_name")
                return None
            if not website:
                logger.debug("Skipping record: empty website for %s", name)
                return None

            # Title-case the name.
            name = name.title()

            # Build extra fields dict (everything except known fields).
            known_keys = {
                "company_name", "website", "city", "state", "country",
                "source_url", "industry_focus", "revenue_tier",
            }
            extra = {
                k: v for k, v in data.items()
                if k not in known_keys and v is not None
            }

            return CompanyResult(
                company_name=name,
                website=website,
                city=city,
                state=state,
                country=country,
                source_url=str(data.get("source_url", "")),
                industry_focus=str(data.get("industry_focus", "")),
                revenue_tier=str(data.get("revenue_tier", "")),
                extra=extra,
            )
        except Exception as exc:
            logger.warning("Failed to normalize record %r: %s", data, exc)
            return None

    def normalize_batch(
        self, records: list[dict[str, Any]]
    ) -> tuple[list[CompanyResult], int]:
        """Normalize a batch of raw company dictionaries.

        Args:
            records: List of raw company dictionaries.

        Returns:
            Tuple of (valid :class:`CompanyResult` list, count of skipped records).
        """
        results: list[CompanyResult] = []
        skipped = 0
        for record in records:
            normalized = self.normalize(record)
            if normalized is not None:
                results.append(normalized)
            else:
                skipped += 1
        return results, skipped

    @staticmethod
    def strip_suffix(name: str) -> str:
        """Remove corporate suffixes from *name* iteratively until stable.

        Args:
            name: Company name that may end with one or more suffixes.

        Returns:
            Name with all trailing suffixes stripped.
        """
        prev = None
        current = name
        while current != prev:
            prev = current
            for suffix in _CORPORATE_SUFFIXES:
                pattern = rf"\s+{re.escape(suffix)}\s*$"
                stripped = re.sub(pattern, "", current, flags=re.IGNORECASE)
                if stripped != current:
                    current = stripped.strip()
                    break
            # Also try single-word suffixes without space prefix
            for suffix in ("Inc", "Ltd", "Co", "Corp"):
                pattern = rf"\b{re.escape(suffix)}$\s*"
                stripped = re.sub(pattern, "", current, flags=re.IGNORECASE)
                if stripped != current:
                    current = stripped.strip()
                    break
        return current

    @staticmethod
    def normalised_name_key(name: str) -> str:
        """Return a deduplication key for a company name.

        Strips corporate suffixes and lowercases the result.

        Args:
            name: Raw company name.

        Returns:
            Lowercased name with suffixes removed.
        """
        return Normalizer.strip_suffix(name).lower().strip()
