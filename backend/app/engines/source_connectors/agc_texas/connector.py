"""AGC Texas Connector.

Discovers construction companies from the Associated General Contractors
of Texas (AGC Texas) member directory.

Note: This connector attempts to fetch live data from AGC Texas website.
If the website is unavailable or returns CAPTCHA, a fallback fixture is used.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from app.engines.source_connectors.agc_texas.config import DEFAULT_CONFIG, AgcTexasConfig
from app.engines.source_connectors.agc_texas.normalizer import AgcTexasNormalizer
from app.engines.source_connectors.agc_texas.parser import AgcTexasParser
from app.engines.source_connectors.sdk import BaseConnector, CompanyResult, ConnectorRegistry

logger = logging.getLogger(__name__)

# Fixture path for offline/fallback mode
_FIXTURE_PATH = Path(__file__).parent / "_fixtures" / "agc_texas_members.json"


class AgcTexasConnector(BaseConnector):
    """Discover construction companies from AGC Texas member directory.

    Connects to https://www.agctexas.org/members/directory to extract
    member company information including names, locations, and services.
    """

    def __init__(self, config: AgcTexasConfig | None = None) -> None:
        """Initialize the AGC Texas connector.

        Args:
            config: Optional configuration. Uses defaults if None.
        """
        super().__init__(config or DEFAULT_CONFIG)
        self._parser = AgcTexasParser()
        self._normalizer = AgcTexasNormalizer()
        self._fallback_data: list[dict[str, Any]] = []
        self._load_fallback()

    def _load_fallback(self) -> None:
        """Load fallback fixture data for testing/offline mode."""
        if not _FIXTURE_PATH.exists():
            self._logger.warning("AGC fixture not found: %s", _FIXTURE_PATH)
            return
        try:
            with open(_FIXTURE_PATH, encoding="utf-8") as f:
                self._fallback_data = json.load(f)
            self._logger.info("Loaded %d fallback AGC members from fixture", len(self._fallback_data))
        except json.JSONDecodeError as exc:
            self._logger.error("Failed to parse AGC fixture: %s", exc)
            self._fallback_data = []

    @property
    def name(self) -> str:
        return "agc_texas"

    @property
    def description(self) -> str:
        return "AGC Texas member directory - construction contractors in Texas"

    def discover(
        self,
        *,
        state: str | None = None,
        city: str | None = None,
        industry: str = "Construction Estimating",
        limit: int = 50,
    ) -> tuple[list[CompanyResult], dict[str, Any]]:
        """Discover AGC Texas member companies.

        Attempts live web scraping first, falls back to fixture data.

        Args:
            state: Filter by state code (default: 'TX').
            city: Filter by city name.
            industry: Industry keyword filter.
            limit: Maximum companies to return.

        Returns:
            (list of CompanyResult, metadata dict)
        """
        self._logger.info(
            "AGC Texas discovery: state=%r city=%r industry=%r limit=%d",
            state, city, industry, limit,
        )

        # Try live scrape first
        companies = self._fetch_live(state, city, limit)

        # Fall back to fixture data
        if not companies:
            self._logger.info("Live fetch returned no results, using fixture data")
            companies = self._apply_filters(self._fallback_data, state, city, industry, limit)

        # Convert to CompanyResult
        results, skipped = self._normalizer.normalize_batch(companies)

        metadata = {
            "connector": self.name,
            "total_in_source": len(self._fallback_data),
            "filtered_count": len(companies),
            "returned_count": len(results),
            "skipped_invalid": skipped,
            "filters_applied": {"state": state, "city": city, "industry": industry},
            "data_source": "live" if companies else "fixture",
        }

        self._logger.info(
            "AGC Texas: %d results from %d candidates (%d skipped)",
            len(results),
            len(companies),
            skipped,
        )
        return results, metadata

    def _fetch_live(
        self,
        state: str | None,
        city: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Attempt to fetch live data from AGC Texas website."""
        try:
            url = f"{self._config.base_url}{self._config.membership_api}"
            self._logger.info("Fetching AGC Texas members from: %s", url)

            response = requests.get(
                url,
                headers={
                    "User-Agent": self._config.user_agent,
                    "Accept": "text/html,application/json",
                },
                timeout=self._config.timeout,
                verify=True,
            )
            response.raise_for_status()

            # Parse HTML
            companies = self._parser.parse_member_list(response.text)
            self._logger.info("Live fetch returned %d companies", len(companies))
            return companies

        except requests.exceptions.HTTPError as exc:
            if exc.response.status_code == 525:
                self._logger.warning("AGC Texas SSL handshake failed (status 525)")
            elif exc.response.status_code == 520:
                self._logger.warning("AGC Texas web server error (status 520)")
            else:
                self._logger.warning("AGC Texas HTTP error: %s", exc)
        except requests.exceptions.ConnectionError as exc:
            self._logger.warning("AGC Texas connection failed: %s", exc)
        except Exception as exc:
            self._logger.error("AGC Texas fetch error: %s", exc, exc_info=True)

        return []

    def _apply_filters(
        self,
        data: list[dict[str, Any]],
        state: str | None,
        city: str | None,
        industry: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Apply filters to raw company data."""
        filtered = data

        # Filter by state
        if state:
            state_upper = state.upper()
            filtered = [c for c in filtered if c.get("state", "").upper() == state_upper]

        # Filter by city
        if city:
            city_lower = city.lower()
            filtered = [c for c in filtered if c.get("city", "").lower() == city_lower]

        # Filter by industry relevance
        if industry and industry.lower() != "all":
            keywords = re.findall(r"[a-z]{3,}", industry.lower())
            filtered = [
                c for c in filtered
                if any(kw in c.get("industry_focus", "").lower() or
                       kw in c.get("company_name", "").lower()
                       for kw in keywords)
            ]

        # Apply limit
        return filtered[:limit]

    def is_available(self) -> bool:
        """Check if AGC Texas website is accessible."""
        try:
            response = requests.get(
                self._config.base_url,
                headers={"User-Agent": self._config.user_agent},
                timeout=10,
                verify=True,
            )
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False


# Register the connector
ConnectorRegistry.register(AgcTexasConnector())
