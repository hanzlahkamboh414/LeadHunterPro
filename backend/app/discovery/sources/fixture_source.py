"""Fixture bridge source — emergency fallback.

Returns curated fixture data when no live sources produce results.
This source ALWAYS succeeds (returns data or empty list) and should
be registered last (lowest priority) so it is only used as a last resort.

Per ADR-002, this source MUST:
1. Log clearly when activated
2. Mark results with data_source="fixture" and temporary=true
3. Never be the primary discovery path in production
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.discovery.sources.base_source import BaseSource

logger = logging.getLogger(__name__)

_FIXTURE_PATH = (
    Path(__file__).parent.parent.parent / "fixtures" / "texas_procurement.json"
)


class FixtureSource(BaseSource):
    """Bridge source that returns fixture data when no live sources succeed.

    This is NOT a primary data source. It exists solely as an emergency
    fallback per ADR-002 until real live sources are integrated.
    """

    source_name = "fixture_bridge"
    description = "Curated fixture dataset (emergency bridge only)"
    priority = 999  # Lowest priority — always last
    enabled = True

    def __init__(self, fixture_path: Path | None = None) -> None:
        """Initialize the fixture source.

        Args:
            fixture_path: Path to the fixture JSON file.
                Defaults to app/fixtures/texas_procurement.json.
        """
        self._path = fixture_path or _FIXTURE_PATH
        self._companies: list[dict[str, Any]] = []
        self._meta: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """Load fixture data from JSON file."""
        if not self._path.exists():
            logger.error("Fixture file not found: %s", self._path)
            self._companies = []
            self._meta = {"data_source": "missing", "temporary": True}
            return

        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self._companies = data.get("companies", [])
            self._meta = {
                "data_source": "fixture",
                "temporary": data.get("temporary", True),
                "version": data.get("version", "unknown"),
                "last_updated": data.get("last_updated", "unknown"),
                "description": data.get("description", ""),
            }
            logger.info(
                "FixtureSource loaded %d companies from %s",
                len(self._companies),
                self._path,
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load fixture %s: %s", self._path, exc)
            self._companies = []
            self._meta = {"data_source": "error", "temporary": True, "error": str(exc)}

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return all fixture companies matching industry + location.

        Args:
            industry: Industry keyword.
            location: Geographic location.
            limit: Maximum results.

        Returns:
            Tuple of (matched companies, metadata).
        """
        from app.connectors.industry_expansion import expand_industry
        from app.connectors.texas_procurement import _parse_location

        city, state = _parse_location(location)
        keywords = expand_industry(industry)

        matched: list[dict[str, Any]] = []
        for company in self._companies:
            if state and company.get("state", "").upper() != state.upper():
                continue
            if city and company.get("city", "").lower() != city.lower():
                continue
            text = (
                f"{company.get('company_name', '')} "
                f"{company.get('industry_focus', '')} "
                f"{company.get('trade_category', '')}"
            ).lower()
            if any(kw in text for kw in keywords):
                matched.append(company)

        logger.warning(
            "FixtureSource returning %d results (BRIDGE DATA — "
            "all live sources failed)",
            len(matched),
        )
        return matched[:limit], {
            **self._meta,
            "total_in_dataset": len(self._companies),
            "total_matched": len(matched),
            "fallback_reason": "no_live_sources_available",
        }

    async def health_check(self) -> dict[str, Any]:
        """Check if fixture data is available."""
        healthy = len(self._companies) > 0
        return {
            "healthy": healthy,
            "source": self.source_name,
            "record_count": len(self._companies),
            "note": "EMERGENCY BRIDGE ONLY — not a real data source",
        }
