"""Connector manager — orchestrates discovery across multiple connectors.

Manages connector lifecycle, executes discovery, and aggregates results.
The manager is responsible for loading enabled connectors, sorting by
priority, executing searches, collecting results, and deduplicating.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.connectors.connector_registry import ConnectorRegistry
from app.connectors.connector_result import ConnectorResult

logger = logging.getLogger(__name__)

# Industry keyword groups for matching (module-level to avoid mutable default)
_INDUSTRY_GROUPS: dict[str, list[str]] = {
    "general_contractor": [
        "general contractor",
        "general contractors",
        "gc",
        "contracting",
        "construction company",
        "construction companies",
        "commercial contractor",
        "commercial contractors",
        "building contractor",
        "building contractors",
    ],
    "commercial_construction": [
        "commercial construction",
        "commercial builder",
        "commercial builders",
        "construction estimating",
        "estimating",
        "cost consulting",
        "project management",
    ],
    "heavy_civil": [
        "heavy civil",
        "civil engineering",
        "infrastructure",
        "highways",
        "roads",
        "bridges",
        "paving",
    ],
    "residential": [
        "residential construction",
        "residential builder",
        "home builder",
        "home builders",
        "remodeling",
        "renovation",
    ],
}


class ConnectorManager:
    """Orchestrates discovery across registered connectors.

    Responsibilities:
    - Load enabled connectors from registry.
    - Sort connectors by priority.
    - Execute connectors sequentially.
    - Collect and merge results.
    - Deduplicate results using domain, name, and fuzzy matching.
    - Score and rank results by relevance.
    - Return normalized ConnectorResult instances.

    The manager NEVER knows connector-specific logic. It operates
    purely on the :class:`BaseConnector` interface.
    """

    def __init__(self) -> None:
        """Initialize the connector manager."""
        self._results: list[ConnectorResult] = []

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[ConnectorResult], dict[str, Any]]:
        """Execute discovery across all enabled connectors.

        Args:
            industry: Industry keyword (e.g. 'Construction Estimating').
            location: Geographic location (e.g. 'Dallas Texas USA').
            limit: Maximum total results to return.

        Returns:
            Tuple of (deduplicated results, execution metadata).
        """
        logger.info(
            "ConnectorManager.discover: industry=%r location=%r limit=%d",
            industry,
            location,
            limit,
        )

        connectors = ConnectorRegistry.get_enabled()
        logger.info("Running %d enabled connectors", len(connectors))

        all_results: list[ConnectorResult] = []
        errors: dict[str, str] = {}
        stats: dict[str, int] = {}

        for connector in connectors:
            connector_name = connector.connector_name
            try:
                logger.info("Executing connector: %s", connector_name)
                results, metadata = connector.search(
                    industry=industry,
                    location=location,
                    limit=limit,
                )

                # Validate each result
                valid_results: list[ConnectorResult] = []
                for result in results:
                    if connector.validate_result(result):
                        valid_results.append(result)

                all_results.extend(valid_results)
                stats[connector_name] = {
                    "total": len(results),
                    "valid": len(valid_results),
                }
                logger.info(
                    "Connector %s: %d total, %d valid",
                    connector_name,
                    len(results),
                    len(valid_results),
                )

            except Exception as exc:  # noqa: BLE001
                errors[connector_name] = str(exc)
                logger.error("Connector %s failed: %s", connector_name, exc)

        logger.info("Raw companies: %d", len(all_results))

        # Deduplicate
        deduped = self._deduplicate(all_results)
        logger.info("After deduplication: %d", len(deduped))

        # Score and rank
        ranked = self._rank_results(deduped, industry, location)
        logger.info("After ranking: %d", len(ranked))

        metadata: dict[str, Any] = {
            "connectors_executed": len(connectors),
            "total_raw": len(all_results),
            "total_deduped": len(deduped),
            "total_ranked": len(ranked),
            "stats": stats,
            "errors": errors,
        }

        logger.info(
            "Discovery complete: %d raw -> %d after dedup -> %d after ranking (%d connectors)",
            len(all_results),
            len(deduped),
            len(ranked),
            len(connectors),
        )
        return ranked[:limit], metadata

    def _deduplicate(self, results: list[ConnectorResult]) -> list[ConnectorResult]:
        """Remove duplicate results based on domain, name, and fuzzy similarity.

        Args:
            results: List of connector results to deduplicate.

        Returns:
            Deduplicated list of results.
        """
        seen_domains: set[str] = set()
        seen_normalized_names: set[str] = set()
        unique: list[ConnectorResult] = []

        for result in results:
            domain = self._extract_domain(result.website)
            norm_name = self._normalize_name(result.company_name)

            # Check for duplicates
            dup_domain = domain in seen_domains
            dup_name = norm_name in seen_normalized_names

            # Also check fuzzy similarity with existing results
            fuzzy_dup = False
            for existing in unique:
                existing_norm = self._normalize_name(existing.company_name)
                if self._is_similar_name(norm_name, existing_norm):
                    fuzzy_dup = True
                    break

            if dup_domain or dup_name or fuzzy_dup:
                logger.debug(
                    "Duplicate removed: %s (domain=%s, name=%s, fuzzy=%s)",
                    result.company_name,
                    dup_domain,
                    dup_name,
                    fuzzy_dup,
                )
                continue

            if not dup_domain:
                seen_domains.add(domain)
            if not dup_name:
                seen_normalized_names.add(norm_name)

            unique.append(result)

        logger.info("Deduplicated: %d -> %d", len(results), len(unique))
        return unique

    def _rank_results(
        self, results: list[ConnectorResult], industry: str, location: str
    ) -> list[ConnectorResult]:
        """Score and rank results by relevance.

        Scoring criteria:
        - +40 points: State match
        - +30 points: City match
        - +25 points: Industry match (using expanded keywords)
        - +10 points: Trusted connector
        - +5 points: Live verified URL
        - +5 points: High revenue tier

        Args:
            results: List of results to rank.
            industry: Search industry.
            location: Search location.

        Returns:
            Sorted list of results by score (highest first).
        """
        from app.connectors.industry_expansion import expand_industry

        parsed_location = _parse_location(location)
        search_city, search_state = parsed_location
        expanded_keywords = expand_industry(industry)

        scored: list[tuple[int, ConnectorResult]] = []

        for result in results:
            score = 0

            # State match (+40)
            if search_state and result.state.upper() == search_state.upper():
                score += 40

            # City match (+30)
            if search_city and result.city.lower() == search_city.lower():
                score += 30

            # Industry match using expanded keywords (+25)
            if self._matches_industry_expanded(result, expanded_keywords):
                score += 25

            # Trusted connector bonus (+10)
            if result.source == "texas_procurement":
                score += 10

            # Live verified URL (+5)
            if result.metadata.get("verified_url"):
                score += 5

            # Revenue tier bonus (+5 for enterprise/large)
            revenue = result.metadata.get("revenue_tier", "")
            if revenue in ("enterprise", "large"):
                score += 5

            scored.append((score, result))

        # Sort by score descending, then by confidence for ties
        scored.sort(key=lambda x: (x[0], x[1].confidence), reverse=True)
        return [r for _, r in scored]

    def _matches_industry(self, result: ConnectorResult, industry: str) -> bool:
        """Check if a result matches the industry search term.

        Uses keyword group matching for better accuracy.

        Args:
            result: The company result to check.
            industry: The industry search term.

        Returns:
            True if the result matches the industry.
        """
        industry_lower = industry.lower()
        focus = result.metadata.get("industry_focus", "").lower()

        # Check if industry keywords appear in focus
        keywords = self._extract_industry_keywords(industry_lower)
        if keywords & set(re.findall(r"[a-z]+", focus)):
            return True

        # Check for exact phrase match
        for keyword in keywords:
            if keyword in focus:
                return True

        return False

    def _matches_industry_expanded(
        self, result: ConnectorResult, expanded_keywords: set[str]
    ) -> bool:
        """Check if result matches using pre-expanded keywords.

        Args:
            result: The company result to check.
            expanded_keywords: Pre-computed expanded keywords.

        Returns:
            True if any expanded keyword appears in company text.
        """
        focus = result.metadata.get("industry_focus", "").lower()
        name = result.company_name.lower()
        text = f"{name} {focus}"

        for kw in expanded_keywords:
            if kw in text:
                return True
        return False

    def _extract_industry_keywords(self, industry: str) -> set[str]:
        """Extract keywords from industry string for matching.

        Args:
            industry: Industry search term.

        Returns:
            Set of lowercase keywords.
        """
        keywords: set[str] = set()

        # Add phrases from industry groups
        for group_keywords in _INDUSTRY_GROUPS.values():
            for kw in group_keywords:
                if kw in industry:
                    keywords.add(kw)

        # Also add individual words (3+ chars)
        words = set(re.findall(r"[a-z]{3,}", industry))
        keywords.update(words)

        return keywords

    def _is_similar_name(self, name1: str, name2: str, threshold: float = 0.7) -> bool:
        """Check if two names are similar enough to be duplicates.

        Args:
            name1: First company name.
            name2: Second company name.
            threshold: Minimum similarity score.

        Returns:
            True if names are considered similar.
        """
        if not name1 or not name2:
            return False

        # Exact match after normalization
        if name1 == name2:
            return True

        # Check if one contains the other
        if name1 in name2 or name2 in name1:
            return True

        # Word overlap
        words1 = set(name1.split())
        words2 = set(name2.split())
        if not words1 or not words2:
            return False

        intersection = words1 & words2
        union = words1 | words2
        jaccard = len(intersection) / len(union) if union else 0.0

        return jaccard >= threshold

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract normalized domain from URL.

        Args:
            url: Website URL.

        Returns:
            Normalized domain string (lowercase, no www.).
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        hostname = hostname.lower().replace("www.", "")
        return hostname.strip()

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Normalize company name for comparison.

        Strips common suffixes and lowercases.

        Args:
            name: Company name.

        Returns:
            Normalized name string.
        """
        import re

        suffixes = r"\s+(Inc\.?|Incorporated|LLC|L\.L\.C\.?|Ltd\.?|Limited|Corp\.?|Corporation|Co\.?|Company|Group|Holdings|Construction|Contractors?|Builders?)\s*$"
        normalized = re.sub(suffixes, "", name, flags=re.IGNORECASE).strip()
        return normalized.lower()


def _parse_location(location: str) -> tuple[str | None, str]:
    """Parse location string into (city, state).

    Args:
        location: Location string (e.g. 'Dallas Texas').

    Returns:
        Tuple of (city, state).
    """
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
    _STATE_NAME_TO_CODE = {v.upper(): k for k, v in _STATE_MAP.items()}

    location = re.sub(r"\s+", " ", location.strip()).lower()

    state = "TX"
    city_loc = location

    # Find full state name
    for state_code, state_name in _STATE_MAP.items():
        pattern = r"\b" + re.escape(state_name.lower()) + r"\b"
        if re.search(pattern, location):
            state = state_code
            city_loc = re.sub(pattern, "", location).strip()
            break

    # Find state code
    if state == "TX":
        for state_code in _STATE_MAP:
            pattern = r"(?:^|[\s,;])" + state_code + r"(?:$|[\s,;])"
            if re.search(pattern, location, re.IGNORECASE):
                state = state_code
                city_loc = re.sub(pattern, " ", location, flags=re.IGNORECASE).strip()
                break

    # Clean up
    for state_name in _STATE_NAME_TO_CODE.values():
        city_loc = re.sub(r"\b" + re.escape(state_name.lower()) + r"\b", "", city_loc)

    city = re.sub(r"^[,\s;]+|[,\s;]+$", "", city_loc).strip() or None
    return city, state
