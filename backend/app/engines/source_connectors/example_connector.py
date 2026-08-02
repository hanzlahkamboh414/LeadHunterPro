"""Example connector using a fake JSON data file.

This connector demonstrates the SDK patterns without connecting to real
external services. It reads from a local JSON fixture file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.engines.source_connectors.sdk import BaseConnector, CompanyResult, ConnectorConfig

logger = logging.getLogger(__name__)

# Path to fake data fixture
_FIXTURE_PATH = Path(__file__).parent / "_fixtures" / "example_companies.json"


class ExampleConnector(BaseConnector):
    """Example connector that reads from a local JSON fixture.

    This connector demonstrates the SDK patterns for development and testing.
    In production, replace with a real data source (API, database, scraper).
    """

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        super().__init__(config)
        self._data: list[dict[str, Any]] = []
        self._load_fixture()

    def _load_fixture(self) -> None:
        """Load fake company data from fixture file."""
        if not _FIXTURE_PATH.exists():
            self._logger.warning("Fixture file not found: %s", _FIXTURE_PATH)
            return
        try:
            with open(_FIXTURE_PATH, encoding="utf-8") as f:
                self._data = json.load(f)
            self._logger.info("Loaded %d example companies from fixture", len(self._data))
        except json.JSONDecodeError as exc:
            self._logger.error("Failed to parse fixture: %s", exc)
            self._data = []

    @property
    def name(self) -> str:
        return "example"

    @property
    def description(self) -> str:
        return "Example connector using local JSON fixture data"

    def discover(
        self,
        *,
        state: str | None = None,
        city: str | None = None,
        industry: str = "Construction Estimating",
        limit: int = 50,
    ) -> tuple[list[CompanyResult], dict[str, Any]]:
        """Discover companies from the fixture data.

        Args:
            state: Filter by state code.
            city: Filter by city name.
            industry: Industry keyword (not used in fixture).
            limit: Max results.

        Returns:
            (list of CompanyResult, metadata dict)
        """
        self._logger.info("ExampleConnector.discover: state=%r city=%r limit=%d", state, city, limit)

        results: list[CompanyResult] = []
        for raw in self._data:
            # Apply filters
            if state and raw.get("state", "").upper() != state.upper():
                continue
            if city and raw.get("city", "").lower() != city.lower():
                continue

            results.append(CompanyResult.from_dict(raw))
            if len(results) >= limit:
                break

        metadata = {
            "connector": self.name,
            "total_records": len(self._data),
            "returned_count": len(results),
            "filters_applied": {"state": state, "city": city, "industry": industry},
        }
        self._logger.info("ExampleConnector: %d results from %d records", len(results), len(self._data))
        return results, metadata


# Generate fixture file if it doesn't exist
def ensure_fixture() -> None:
    """Create the fixture directory and sample data file."""
    _FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _FIXTURE_PATH.exists():
        return

    sample_data = [
        {
            "company_name": "Acme Construction LLC",
            "website": "https://www.acmeconstruction.example.com",
            "city": "Dallas",
            "state": "TX",
            "country": "USA",
            "source_url": "https://www.acmeconstruction.example.com/about",
            "industry_focus": "Commercial construction, general contracting",
            "revenue_tier": "mid-market",
        },
        {
            "company_name": "Lone Star Builders Inc.",
            "website": "https://www.lonestarbuilders.example.com",
            "city": "Houston",
            "state": "TX",
            "country": "USA",
            "source_url": "https://www.lonestarbuilders.example.com",
            "industry_focus": "Residential construction, renovations",
            "revenue_tier": "small",
        },
        {
            "company_name": "Texas General Contractors",
            "website": "https://www.texasgeneral.example.com",
            "city": "Austin",
            "state": "TX",
            "country": "USA",
            "source_url": "https://www.texasgeneral.example.com/contact",
            "industry_focus": "General contracting, project management",
            "revenue_tier": "large",
        },
    ]

    with open(_FIXTURE_PATH, "w", encoding="utf-8") as f:
        json.dump(sample_data, f, indent=2)
    logger.info("Created example fixture at %s", _FIXTURE_PATH)
