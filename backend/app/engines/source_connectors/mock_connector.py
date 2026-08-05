"""Mock connector for testing the SDK without external dependencies.

Returns a fixed set of synthetic company records, ideal for unit tests
and integration tests that need deterministic results.
"""

from __future__ import annotations

import logging
from typing import Any

from app.engines.source_connectors.sdk import BaseConnector, CompanyResult, ConnectorConfig

logger = logging.getLogger(__name__)

# Synthetic company data for testing.
_MOCK_COMPANIES: list[dict[str, Any]] = [
    {
        "company_name": "Apex Construction LLC",
        "website": "https://www.apexconstruction.example.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.apexconstruction.example.com",
        "industry_focus": "Commercial construction, general contracting",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Summit Builders Inc.",
        "website": "https://www.summitbuilders.example.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.summitbuilders.example.com/about",
        "industry_focus": "Residential construction, renovations",
        "revenue_tier": "small",
    },
    {
        "company_name": "Lone Star Contractors",
        "website": "https://www.lonestarcontractors.example.com",
        "city": "Austin",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.lonestarcontractors.example.com/contact",
        "industry_focus": "General contracting, project management",
        "revenue_tier": "large",
    },
    {
        "company_name": "Texas Steel Structures",
        "website": "https://www.texassteel.example.com",
        "city": "San Antonio",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.texassteel.example.com",
        "industry_focus": "Steel fabrication, structural",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Gulf Coast Engineering",
        "website": "https://www.gulfcoasteng.example.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.gulfcoasteng.example.com/services",
        "industry_focus": "Civil engineering, infrastructure",
        "revenue_tier": "enterprise",
    },
    # Duplicate by domain (should be filtered by deduplicator)
    {
        "company_name": "Apex Construction Group",
        "website": "https://www.apexconstruction.example.com/locations/dallas",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.apexconstruction.example.com/locations/dallas",
        "industry_focus": "Commercial",
        "revenue_tier": "mid-market",
    },
    # Duplicate by normalised name (Apex Construction LLC vs Apex Construction)
    {
        "company_name": "Apex Construction",
        "website": "https://www.apex-build.example.com",
        "city": "Fort Worth",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.apex-build.example.com",
        "industry_focus": "General contracting",
        "revenue_tier": "small",
    },
]


class MockConnector(BaseConnector):
    """Mock connector that returns synthetic company data.

    Use this connector in tests to avoid hitting real external services.
    It does not perform any network requests.
    """

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        """Initialize the mock connector.

        Args:
            config: Optional configuration. Defaults are used if not provided.
        """
        super().__init__(config)
        self._data = _MOCK_COMPANIES

    @property
    def name(self) -> str:
        return "mock"

    @property
    def description(self) -> str:
        return "Mock connector returning synthetic construction company data for testing"

    def discover(
        self,
        *,
        state: str | None = None,
        city: str | None = None,
        industry: str = "Construction Estimating",
        limit: int = 50,
    ) -> tuple[list[CompanyResult], dict[str, Any]]:
        """Return mock company results, optionally filtered.

        Args:
            state: Filter by state code.
            city: Filter by city name.
            industry: Industry keyword (not actively filtered in mock).
            limit: Maximum results.

        Returns:
            Tuple of (list of CompanyResult, metadata dict).
        """
        self._logger.info(
            "MockConnector.discover: state=%r city=%r limit=%d",
            state, city, limit,
        )

        results: list[CompanyResult] = []
        for raw in self._data:
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
            "filters_applied": {
                "state": state,
                "city": city,
                "industry": industry,
            },
            "is_mock": True,
        }
        self._logger.info("MockConnector: %d results from %d records", len(results), len(self._data))
        return results, metadata
