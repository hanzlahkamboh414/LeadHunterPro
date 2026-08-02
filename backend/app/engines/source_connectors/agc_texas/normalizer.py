"""AGC Texas data normalizer.

Normalizes parsed company data into standardized CompanyResult format.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.engines.source_connectors.sdk import CompanyResult

logger = logging.getLogger(__name__)


class AgcTexasNormalizer:
    """Normalize AGC Texas data to CompanyResult format."""

    # State abbreviation mapping
    STATE_ABBR: dict[str, str] = {
        "texas": "TX",
        "tx": "TX",
    }

    # Revenue tier classification based on company size indicators
    REVENUE_TIERS = {
        "enterprise": ["national", "multi-state", "fortune", "top 400"],
        "large": ["regional", "major", "big"],
        "mid-market": ["mid-size", "mid market", "medium"],
        "small": ["small", "local", " boutique"],
    }

    def normalize(self, raw_data: dict[str, Any]) -> CompanyResult | None:
        """Normalize raw company data to CompanyResult.

        Args:
            raw_data: Parsed company data dictionary.

        Returns:
            Normalized CompanyResult or None if invalid.
        """
        try:
            # Validate required fields
            if not raw_data.get("company_name") or not raw_data.get("city"):
                logger.debug("Skipping incomplete company data: %s", raw_data)
                return None

            # Normalize state
            state = self._normalize_state(raw_data.get("state", "TX"))

            # Normalize company name
            company_name = self._normalize_name(raw_data.get("company_name", ""))

            # Extract website if provided
            website = raw_data.get("website", "")
            if website and not website.startswith("http"):
                website = f"https://{website}"

            # Determine revenue tier
            revenue_tier = self._classify_revenue_tier(raw_data)

            # Build result
            return CompanyResult(
                company_name=company_name,
                website=website,
                city=raw_data.get("city", "").strip(),
                state=state,
                country=raw_data.get("country", "USA"),
                source_url=raw_data.get("source_url", ""),
                industry_focus=raw_data.get("industry_focus", "General contracting"),
                revenue_tier=revenue_tier,
            )

        except Exception as exc:
            logger.warning("Failed to normalize company data: %s", exc)
            return None

    def normalize_batch(self, raw_data_list: list[dict[str, Any]]) -> tuple[list[CompanyResult], int]:
        """Normalize a batch of raw company data.

        Args:
            raw_data_list: List of raw company dictionaries.

        Returns:
            Tuple of (normalized results, count of skipped records).
        """
        results: list[CompanyResult] = []
        skipped = 0

        for raw in raw_data_list:
            normalized = self.normalize(raw)
            if normalized:
                results.append(normalized)
            else:
                skipped += 1

        logger.info(
            "Normalized %d/%d records (%d skipped)",
            len(results),
            len(raw_data_list),
            skipped,
        )
        return results, skipped

    @staticmethod
    def _normalize_state(state: str) -> str:
        """Normalize state to two-letter abbreviation."""
        state_lower = state.lower().strip()
        return AgcTexasNormalizer.STATE_ABBR.get(state_lower, state.upper()[:2])

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Clean up company name formatting."""
        # Remove extra whitespace
        name = re.sub(r"\s+", " ", name).strip()
        # Remove common suffixes for consistency (but keep in original)
        suffixes_to_remove = [
            " LLC", " L.L.C.", " Inc.", " Incorporated",
            " Corp.", " Corporation", " Co.", " Company",
        ]
        for suffix in suffixes_to_remove:
            if name.endswith(suffix):
                name = name[:-len(suffix)].strip()
                break
        return name.title()

    def _classify_revenue_tier(self, data: dict[str, Any]) -> str:
        """Classify revenue tier based on available indicators."""
        name = data.get("company_name", "").lower()
        description = data.get("description", "").lower()
        combined = f"{name} {description}"

        for tier, keywords in self.REVENUE_TIERS.items():
            if any(kw in combined for kw in keywords):
                return tier

        # Default based on available data
        if data.get("employees"):
            emp = int(data["employees"])
            if emp > 1000:
                return "enterprise"
            elif emp > 100:
                return "large"
            elif emp > 20:
                return "mid-market"
        return "small"
