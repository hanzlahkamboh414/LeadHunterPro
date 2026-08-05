"""Adapters to migrate legacy connectors to new BaseConnector interface.

This module provides adapter classes that wrap existing legacy connectors
(TexasProcurementConnector, AgcTexasConnector) to conform to the new
BaseConnector interface without modifying the original connector code.

Built-in adapters are automatically registered on import.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests

from app.connectors.base_connector import BaseConnector
from app.connectors.connector_registry import ConnectorRegistry
from app.connectors.connector_result import ConnectorResult
from app.engines.source_connectors.agc_texas import AgcTexasConnector
from app.engines.source_connectors.texas_procurement import TexasProcurementConnector

logger = logging.getLogger(__name__)

# State name to code mapping for location parsing
_STATE_MAP = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

# Reverse mapping: full name -> code
_STATE_NAME_TO_CODE = {v.upper(): k for k, v in _STATE_MAP.items()}


def _normalize_text(text: str) -> str:
    """Normalize text by trimming, lowercasing, and collapsing whitespace."""
    if not text:
        return ""
    # Remove invisible characters and normalize whitespace
    text = re.sub(r"\s+", " ", text.strip())
    return text.lower()


def _parse_location(location: str) -> tuple[str | None, str]:
    """Parse location string into (city, state).

    Handles formats like:
    - "Dallas Texas" -> ("dallas", "TX")
    - "Houston, TX" -> ("houston", "TX")
    - "Dallas" -> (None, "TX")  # default to TX for Texas Procurement
    - "TX" -> (None, "TX")

    Args:
        location: Location string from API query.

    Returns:
        Tuple of (city, state) where city may be None.
    """
    location = _normalize_text(location)

    state = "TX"  # Default state
    city_loc = location

    # First, try to find full state name (e.g., "texas")
    for state_code, state_name in _STATE_MAP.items():
        pattern = r"\b" + re.escape(state_name.lower()) + r"\b"
        if re.search(pattern, location):
            state = state_code
            city_loc = re.sub(pattern, "", location).strip()
            break

    # Then, try to find state code abbreviation (e.g., "tx")
    if state == "TX":  # Only if not already found via full name
        for state_code in _STATE_MAP:
            # Match as standalone word with flexible separators
            pattern = r"(?:^|[\s,;])" + state_code + r"(?:$|[\s,;])"
            if re.search(pattern, location, re.IGNORECASE):
                state = state_code
                city_loc = re.sub(pattern, " ", location, flags=re.IGNORECASE).strip()
                break

    # Clean up city: remove any remaining state-related text
    for state_name in _STATE_NAME_TO_CODE.values():
        city_loc = re.sub(r"\b" + re.escape(state_name.lower()) + r"\b", "", city_loc)

    # Final cleanup
    city = re.sub(r"^[,\s;]+|[,\s;]+$", "", city_loc).strip() or None

    logger.info("Parsed location %r -> city=%r, state=%s", location, city, state)
    return city, state


def _verify_url(url: str, timeout: int = 5) -> bool:
    """Check if a URL returns HTTP 200.

    Args:
        url: URL to verify.
        timeout: Request timeout in seconds.

    Returns:
        True if URL returns 2xx status.
    """
    if not url:
        return False
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        return 200 <= resp.status_code < 300
    except requests.RequestException:
        return False


def _get_base_url(url: str) -> str:
    """Extract base URL (domain + path to homepage) from a URL.

    Args:
        url: Full URL.

    Returns:
        Base URL without trailing path segments.
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    # Keep first path segment if it looks like a directory
    if parsed.path and parsed.path != "/":
        parts = parsed.path.strip("/").split("/")
        if parts:
            base = f"{base}/{parts[0]}"
    return base


class TexasProcurementAdapter(BaseConnector):
    """Adapter for TexasProcurementConnector to new BaseConnector interface."""

    connector_name = "texas_procurement"
    priority = 10
    enabled = True

    def __init__(self) -> None:
        self._legacy = TexasProcurementConnector()

    def search(
        self, industry: str, location: str, limit: int
    ) -> tuple[list[ConnectorResult], dict[str, Any]]:
        """Execute discovery via Texas procurement connector with URL verification.

        Args:
            industry: Industry keyword.
            location: Geographic location (e.g. 'Dallas Texas').
            limit: Maximum companies to return.

        Returns:
            Tuple of (list of ConnectorResult, metadata dict).
        """
        # Parse location into city and state
        city, state = _parse_location(location)

        logger.info(
            "TexasProcurementAdapter.search: industry=%r city=%r state=%s limit=%d",
            industry,
            city,
            state,
            limit,
        )

        try:
            # Call legacy connector with parsed parameters
            results, metadata = self._legacy.discover(
                state=state,
                city=city,
                industry=industry,
                limit=limit,
            )

            logger.info(
                "TexasProcurementAdapter: raw=%d state_filter->%d city_filter->%d",
                metadata.get("total_records", 0),
                metadata.get("filtered_count", 0),
                metadata.get("returned_count", 0),
            )

            # Convert and verify URLs
            connector_results: list[ConnectorResult] = []
            now = datetime.utcnow().isoformat() + "Z"

            for r in results:
                # Handle both dict and CompanyResult types
                if hasattr(r, "to_dict"):
                    d = r.to_dict()
                else:
                    d = r

                company_name = d.get("company_name", "")
                website = d.get("website", "")
                source_url = d.get("source_url", "")

                # Verify URLs
                website_verified = _verify_url(website)
                source_url_verified = _verify_url(source_url) if source_url else False

                # Replace broken source_url with verified website if needed
                final_source_url = source_url
                if source_url and not source_url_verified:
                    logger.info(
                        "Source URL 404 for %s: %s -> replacing with %s",
                        company_name,
                        source_url,
                        website,
                    )
                    final_source_url = website
                    source_url_verified = website_verified

                connector_results.append(
                    ConnectorResult(
                        company_name=company_name,
                        website=website,
                        city=d.get("city", ""),
                        state=d.get("state", ""),
                        country=d.get("country", "USA"),
                        source=self.connector_name,
                        source_url=final_source_url,
                        confidence=0.8,
                        metadata={
                            "source_url_original": source_url,
                            "website_verified": website_verified,
                            "source_url_verified": source_url_verified,
                            "verification_timestamp": now,
                            **{
                                k: v
                                for k, v in d.items()
                                if k
                                not in {
                                    "company_name",
                                    "website",
                                    "city",
                                    "state",
                                    "country",
                                    "source_url",
                                }
                            },
                        },
                    )
                )

            logger.info(
                "TexasProcurementAdapter: verified %d/%d URLs",
                sum(1 for r in connector_results if r.metadata.get("website_verified")),
                len(connector_results),
            )
            return connector_results, metadata

        except Exception as exc:
            logger.error(
                "TexasProcurementAdapter.search failed: %s", exc, exc_info=True
            )
            return [], {"error": str(exc)}

    def health_check(self) -> bool:
        """Check if Texas procurement source is available.

        Returns:
            True if connector is available.
        """
        return self._legacy.is_available()

    def validate_result(self, result: ConnectorResult) -> bool:
        """Validate a ConnectorResult from Texas procurement.

        Args:
            result: The connector result to validate.

        Returns:
            True if result has valid company_name and website.
        """
        return bool(result.company_name and result.website)


class AgcTexasAdapter(BaseConnector):
    """Adapter for AgcTexasConnector to new BaseConnector interface."""

    connector_name = "agc_texas"
    priority = 20
    enabled = True

    def __init__(self) -> None:
        self._legacy = AgcTexasConnector()

    def search(
        self, industry: str, location: str, limit: int
    ) -> tuple[list[ConnectorResult], dict[str, Any]]:
        """Execute discovery via AGC Texas connector with URL verification.

        Args:
            industry: Industry keyword.
            location: Geographic location.
            limit: Maximum companies to return.

        Returns:
            Tuple of (list of ConnectorResult, metadata dict).
        """
        # Parse location
        city, state = _parse_location(location)

        logger.info(
            "AgcTexasAdapter.search: industry=%r city=%r state=%s limit=%d",
            industry,
            city,
            state,
            limit,
        )

        try:
            results, metadata = self._legacy.discover(
                state=state,
                city=city,
                industry=industry,
                limit=limit,
            )

            logger.info(
                "AgcTexasAdapter: raw=%d filtered=%d returned=%d",
                metadata.get("total_in_source", 0),
                metadata.get("filtered_count", 0),
                metadata.get("returned_count", 0),
            )

            # Convert and verify URLs
            connector_results: list[ConnectorResult] = []
            now = datetime.utcnow().isoformat() + "Z"

            for r in results:
                # Handle both dict and CompanyResult types
                if hasattr(r, "to_dict"):
                    d = r.to_dict()
                else:
                    d = r

                company_name = d.get("company_name", "")
                website = d.get("website", "")
                source_url = d.get("source_url", "")

                # Verify URLs
                website_verified = _verify_url(website)
                source_url_verified = _verify_url(source_url) if source_url else False

                # Replace broken source_url with verified website if needed
                final_source_url = source_url
                if source_url and not source_url_verified:
                    logger.info(
                        "Source URL 404 for %s: %s -> replacing with %s",
                        company_name,
                        source_url,
                        website,
                    )
                    final_source_url = website
                    source_url_verified = website_verified

                connector_results.append(
                    ConnectorResult(
                        company_name=company_name,
                        website=website,
                        city=d.get("city", ""),
                        state=d.get("state", ""),
                        country=d.get("country", "USA"),
                        source=self.connector_name,
                        source_url=final_source_url,
                        confidence=0.7,
                        metadata={
                            "source_url_original": source_url,
                            "website_verified": website_verified,
                            "source_url_verified": source_url_verified,
                            "verification_timestamp": now,
                            **{
                                k: v
                                for k, v in d.items()
                                if k
                                not in {
                                    "company_name",
                                    "website",
                                    "city",
                                    "state",
                                    "country",
                                    "source_url",
                                }
                            },
                        },
                    )
                )

            logger.info(
                "AgcTexasAdapter: verified %d/%d URLs",
                sum(1 for r in connector_results if r.metadata.get("website_verified")),
                len(connector_results),
            )
            return connector_results, metadata

        except Exception as exc:
            logger.error("AgcTexasAdapter.search failed: %s", exc, exc_info=True)
            return [], {"error": str(exc)}

    def health_check(self) -> bool:
        """Check if AGC Texas source is available.

        Returns:
            True if connector is available.
        """
        return self._legacy.is_available()

    def validate_result(self, result: ConnectorResult) -> bool:
        """Validate a ConnectorResult from AGC Texas.

        Args:
            result: The connector result to validate.

        Returns:
            True if result has valid company_name and website.
        """
        return bool(result.company_name and result.website)


# ---------------------------------------------------------------------------
# Auto-registration on import
# ---------------------------------------------------------------------------

ConnectorRegistry.register(TexasProcurementAdapter())
ConnectorRegistry.register(AgcTexasAdapter())

logger.info("Registered %d built-in connector adapters", 2)
