"""Texas Procurement Connector — Production Implementation.

Discovers construction companies from Texas public procurement data,
trade directories, and contractor registries. Uses the Universal
Crawler infrastructure for any live web fetching.

Data flow (production pipeline):
    discover()
        ↓
    _fetch_live()           ← Uses HTTPCrawler for Tier 2 sources
        ↓
    if live results exist:  ← Primary path (Sprint 2.3+)
        use them
    else:                   ← Bridge path (Sprint 2.2)
        load fixture        ← Temporary bridge per ADR-002
        ↓
    normalize
        ↓
    validate
        ↓
    rank
        ↓
    return

The fixture dataset is a TIME-LIMITED BRIDGE per ADR-002.
It MUST be replaced with live sourcing in Sprint 2.3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.connectors.base_connector import BaseConnector
from app.connectors.connector_result import ConnectorResult
from app.connectors.industry_expansion import expand_industry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixture Data Location
# ---------------------------------------------------------------------------
# BRIDGE DATA: This is a temporary bridge per ADR-002.
# Must be replaced with live Tier 2 sources in Sprint 2.3.
# ---------------------------------------------------------------------------

_FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "texas_procurement.json"


def _load_fixture_data() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load fixture data from JSON file.

    Returns:
        Tuple of (companies list, metadata dict with bridge info).
    """
    if not _FIXTURE_PATH.exists():
        logger.error("Fixture file missing: %s", _FIXTURE_PATH)
        return [], {"data_source": "missing", "temporary": True}

    try:
        with open(_FIXTURE_PATH, encoding="utf-8") as f:
            data = json.load(f)

        companies = data.get("companies", [])
        meta = {
            "data_source": "fixture",
            "temporary": data.get("temporary", True),
            "version": data.get("version", "unknown"),
            "last_updated": data.get("last_updated", "unknown"),
            "description": data.get("description", ""),
        }
        logger.info(
            "Loaded %d companies from fixture: %s", len(companies), _FIXTURE_PATH
        )
        return companies, meta
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load fixture %s: %s", _FIXTURE_PATH, exc)
        return [], {"data_source": "error", "temporary": True, "error": str(exc)}


class TexasProcurementConnector(BaseConnector):
    """Discovery connector for Texas construction companies.

    Production pipeline:
    1. Attempt live fetch via _fetch_live() (uses HTTPCrawler)
    2. If no live results, fall back to curated fixtures
    3. Apply industry expansion matching
    4. Validate URLs and company data
    5. Rank by relevance
    6. Return ConnectorResults with metadata

    The fixture fallback is a TIME-LIMITED BRIDGE (ADR-002).
    Sprint 2.3 MUST replace this with live Tier 2 sources.
    """

    connector_name = "texas_procurement"
    priority = 10
    enabled = True

    def __init__(self) -> None:
        """Initialize the Texas Procurement connector."""
        self._companies, self._fixture_meta = _load_fixture_data()
        self._live_results: list[dict[str, Any]] = []
        logger.info(
            "TexasProcurementConnector initialized: %d fixture records, live=%s",
            len(self._companies),
            bool(self._live_results),
        )

    def search(
        self,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[ConnectorResult], dict[str, Any]]:
        """Execute discovery following production pipeline.

        Uses SourceOrchestrator which:
        1. Runs registered discovery plugins first (when any are registered)
        2. Tries SearchProviderSource (live web search) next
        3. Falls back to FixtureSource when no providers are configured
           or all fail
        4. Aggregates, deduplicates, and classifies results
        5. Applies industry expansion matching
        6. Validates URLs and ranks by confidence

        Args:
            industry: Industry search term (e.g. "Roofing").
            location: Geographic location (e.g. "Dallas Texas").
            limit: Maximum number of results to return.

        Returns:
            Tuple of (list of ConnectorResult, metadata dict).
        """
        logger.info(
            "TexasProcurementConnector.search: industry=%r location=%r limit=%d",
            industry,
            location,
            limit,
        )

        # Step 1: Run SourceOrchestrator (plugins + live search + fixture fallback)
        from app.discovery.plugins.base_plugin import PluginCapability
        from app.discovery.source_orchestrator import SourceOrchestrator
        from app.discovery.sources.directory_crawl_source import DirectoryCrawlSource
        from app.discovery.sources.fixture_source import FixtureSource
        from app.discovery.sources.plugin_source import attach_plugin_source
        from app.discovery.sources.search_provider_source import SearchProviderSource
        from app.discovery.website.registration import register_website_discovery

        # Register the Direct Website Discovery plugin before the
        # orchestrator is assembled. Config-gated and idempotent: with no
        # live search origin configured it is a logged no-op, and the
        # pipeline below runs exactly as it did before (CLAUDE.md §1, §5).
        register_website_discovery()

        orchestrator = SourceOrchestrator()
        # Plugin framework, attached only when a company-discovery plugin is
        # registered. With an empty registry this is a logged no-op and the
        # pipeline below is unchanged. COMPANY_DISCOVERY is passed explicitly
        # so a future leadership/email plugin cannot leak into this stage.
        attach_plugin_source(
            orchestrator,
            capability=PluginCapability.COMPANY_DISCOVERY,
        )
        # DirectoryCrawlSource (priority 20) runs BEFORE search providers
        # (50) and the fixture bridge (999): it is the API-free primary
        # path (Blueprint §6 Phase 3). When it returns UNAVAILABLE — e.g.
        # the sandbox blocks seed-host DNS — the orchestrator simply
        # continues with the next source; it is never terminal.
        orchestrator.register(DirectoryCrawlSource())
        orchestrator.register(SearchProviderSource())
        orchestrator.register(FixtureSource())
        companies, orch_metadata = orchestrator.discover(
            industry=industry,
            location=location,
            limit=limit,
        )
        data_source = orch_metadata.get("data_source", "empty")

        # Step 2: Parse location and expand industry for matching
        city, state = _parse_location(location)
        expanded_keywords = expand_industry(industry)
        logger.debug(
            "Expanded industry %r to %d keywords", industry, len(expanded_keywords)
        )

        # Step 3: Apply local filtering (state, city, keyword match).
        # Project heterogeneous source dicts onto the connector's expected
        # keys first so no record is dropped for schema reasons: the website
        # discovery plugin emits name/services/evidence and no location,
        # while search/fixture sources emit company_name/industry_focus.
        # Missing location is filled from the queried state/city — the same
        # heuristic SearchProviderSource applies, and only when the source
        # supplied no location of its own (CLAUDE.md §1 provenance kept).
        companies = [
            self._normalize_company(c, state=state, city=city) for c in companies
        ]
        matched: list[dict[str, Any]] = []
        for company in companies:
            if state and company.get("state", "").upper() != state.upper():
                continue
            if city and company.get("city", "").lower() != city.lower():
                continue
            text = (
                f"{company.get('company_name', '')} "
                f"{company.get('industry_focus', '')} "
                f"{company.get('trade_category', '')}"
            ).lower()
            if any(kw in text for kw in expanded_keywords):
                matched.append(company)

        logger.info(
            "Matched %d companies for industry=%r in location=%r",
            len(matched),
            industry,
            location,
        )

        # Step 4: Build results with validation and ranking
        results: list[ConnectorResult] = []
        discovery_reasons: list[str] = []

        for company in matched[:limit]:
            result, reason = self._build_result(company, industry, expanded_keywords)
            results.append(result)
            discovery_reasons.append(reason)

        # Step 5: Compile metadata
        metadata: dict[str, Any] = {
            "connector": self.connector_name,
            "total_in_dataset": len(companies),
            "total_matched": len(matched),
            "total_returned": len(results),
            "data_source": data_source,
            "source_metadata": orch_metadata,
            "filters_applied": {
                "state": state,
                "city": city,
                "industry": industry,
                "expanded_keywords": list(expanded_keywords),
                "limit": limit,
            },
            "discovery_reasons_sample": discovery_reasons[:5],
        }

        logger.info(
            "TexasProcurementConnector: %d returned from %d matched " "(source=%s)",
            len(results),
            len(matched),
            data_source,
        )
        return results, metadata

    def _fetch_live(
        self,
        industry: str,
        location: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch live company data using SearchProviderManager.

        Executes a web search via configured search providers (SearXNG,
        Brave), then classifies each result as a contractor or rejects
        it. Falls back to empty list when no providers are configured
        or all return no results.

        Args:
            industry: Industry search term (e.g. "Roofing").
            location: Geographic location (e.g. "Dallas Texas").
            limit: Max results to request.

        Returns:
            List of company dicts from live sources, or empty list.
        """

        from app.search_providers.contractor_classifier import (
            ContractorClassifier,
        )
        from app.search_providers.manager import SearchProviderManager
        from app.search_providers.models import SearchQuery
        from app.search_providers.registry import get_registry

        registry = get_registry()
        enabled = registry.get_enabled()
        logger.info(
            "[Stage 1] Registry.get_enabled() -> %d providers",
            len(enabled),
        )
        logger.info(
            "[Stage 2] Provider names: %s",
            [p.provider_name for p in enabled],
        )
        if not enabled:
            logger.info("[Stage 7] No providers registered — returning [] immediately")
            return []

        # Build intelligent search query
        city, state = _parse_location(location)
        search_query = f"{industry} contractor {city or ''} {state}".strip()
        logger.info(
            "_fetch_live: searching %r with %d providers",
            search_query,
            len(enabled),
        )

        query = SearchQuery(
            keywords=search_query,
            num_results=limit
            * 3,  # Request extra to compensate for classification filtering
        )

        manager = SearchProviderManager(registry)
        logger.info("[Stage 5] Calling SearchProviderManager.search()...")
        try:
            response = asyncio.run(manager.search(query))
            logger.info(
                "[Stage 6] SearchResponse: status=%r provider=%r "
                "results=%d error=%r",
                response.status,
                response.provider,
                len(response.results),
                response.error[:100] if response.error else "",
            )
        except Exception:
            logger.exception("_fetch_live: search failed")
            return []

        if not response.results:
            if response.error:
                logger.info(
                    "[Stage 7] Zero results because: provider error -> %r",
                    response.error[:200],
                )
            else:
                logger.info(
                    "[Stage 7] Zero results because: no search results from providers"
                )
            return []

        # Classify and filter results
        classifier = ContractorClassifier()
        companies: list[dict[str, Any]] = []

        for result in response.results:
            classification = classifier.classify(
                name=result.title,
                title=result.title,
                description=result.snippet,
                url=result.url,
                industry_hint=industry,
            )

            if not classification["accepted"]:
                logger.debug(
                    "Rejected: %s — %s", result.title, classification["reject_reason"]
                )
                continue

            companies.append(
                {
                    "company_name": result.title,
                    "website": result.url,
                    "city": city or "",
                    "state": state,
                    "country": "USA",
                    "trade_category": classification["trade_category"],
                    "industry_focus": result.snippet or result.title,
                    "revenue_tier": "",
                    "source_url": result.url,
                    "data_provenance": f"live:{result.position}",
                    "discovery_reason": classification.get("reject_reason", ""),
                }
            )

        logger.info(
            "_fetch_live: %d accepted out of %d search results",
            len(companies),
            len(response.results),
        )
        return companies

    def _normalize_company(
        self,
        company: dict[str, Any],
        *,
        state: str,
        city: str,
    ) -> dict[str, Any]:
        """Project a source company dict onto the connector's expected schema.

        The orchestrator aggregates heterogeneous dicts: search and fixture
        sources emit ``company_name``/``industry_focus``/``trade_category``
        and their own location, while the website discovery plugin emits
        ``name``/``services``/``evidence`` and no location fields. This
        adapter maps every variant onto the keys ``_build_result`` and the
        Step-3 filters consume, so plugin records are not silently dropped.

        Location is filled from the queried *state*/*city* only when the
        source supplied none — the same heuristic SearchProviderSource
        applies to its search results. Provenance is preserved by aliasing
        the plugin's ``discovered_by`` into ``data_provenance``.

        Args:
            company: Raw company dict from any source.
            state: Query-parsed state code (e.g. ``"TX"``).
            city: Query-parsed city (may be empty).

        Returns:
            A dict in the connector's expected schema (originals intact).
        """
        normalized = dict(company)
        services = company.get("services", [])
        services_text = (
            " ".join(services) if isinstance(services, list) else str(services or "")
        ).strip()
        normalized["company_name"] = company.get("company_name") or company.get(
            "name", ""
        )
        normalized["industry_focus"] = (
            company.get("industry_focus") or services_text or company.get("title", "")
        )
        normalized["trade_category"] = company.get("trade_category", "")
        normalized["state"] = company.get("state") or state
        normalized["city"] = company.get("city") or city
        normalized["data_provenance"] = company.get("data_provenance") or company.get(
            "discovered_by", ""
        )
        return normalized

    def _build_result(
        self,
        company: dict[str, Any],
        search_industry: str,
        expanded_keywords: set[str],
    ) -> tuple[ConnectorResult, str]:
        """Build a ConnectorResult with meaningful discovery reason.

        Args:
            company: Raw company data dictionary.
            search_industry: Original industry search term.
            expanded_keywords: Set of expanded keywords used for matching.

        Returns:
            Tuple of (ConnectorResult, discovery_reason_string).
        """
        trade_cat = company.get("trade_category", "")
        city = company.get("city", "")
        industry_focus = company.get("industry_focus", "")
        website = company.get("website", "")

        # Generate discovery reason
        reason = self._generate_discovery_reason(
            trade_cat=trade_cat,
            city=city,
            industry_focus=industry_focus,
            search_industry=search_industry,
        )

        # Validate and clean URL
        verified_url = _verify_and_clean_url(website)
        confidence = 0.85 if verified_url else 0.60

        result = ConnectorResult(
            company_name=company.get("company_name", ""),
            website=verified_url or website,
            city=city,
            state=company.get("state", "TX"),
            country=company.get("country", "USA"),
            source=self.connector_name,
            source_url=company.get("source_url", website),
            confidence=confidence,
            metadata={
                "industry_focus": industry_focus,
                "revenue_tier": company.get("revenue_tier", ""),
                "trade_category": trade_cat,
                "data_provenance": company.get("data_provenance", ""),
                "verified_url": bool(verified_url),
                "matched_keywords": self._find_matched_keywords(
                    company, expanded_keywords
                ),
                "discovery_reason": reason,
            },
        )
        return result, reason

    def _find_matched_keywords(
        self,
        company: dict[str, Any],
        expanded_keywords: set[str],
    ) -> list[str]:
        """Find which expanded keywords matched this company.

        Args:
            company: Company data dictionary.
            expanded_keywords: Set of expanded keywords.

        Returns:
            List of matching keywords (max 3).
        """
        text = (
            f"{company.get('company_name', '')} " f"{company.get('industry_focus', '')}"
        ).lower()
        matched = [kw for kw in expanded_keywords if kw in text]
        return matched[:3]

    def _generate_discovery_reason(
        self,
        trade_cat: str,
        city: str,
        industry_focus: str,
        search_industry: str,
    ) -> str:
        """Generate a human-readable discovery reason.

        Args:
            trade_cat: Company's trade category.
            city: Company's city.
            industry_focus: Company's stated industry focus.
            search_industry: Original user search term.

        Returns:
            Human-readable reason string.
        """
        city_part = f" {city}" if city else ""
        trade_label = (
            trade_cat.replace("_", " ").title()
            if trade_cat
            else search_industry.title()
        )
        search_title = search_industry.title()

        if trade_cat:
            return f"Matched {city_part.strip()} {trade_label.lower()} contractor"

        if industry_focus:
            focus_words = set(re.findall(r"[a-z]+", industry_focus.lower()))
            search_words = set(re.findall(r"[a-z]+", search_industry.lower()))
            overlap = focus_words & search_words
            if overlap:
                best_match = max(overlap, key=len)
                return f"Matched {city_part.strip()} {best_match.title()} contractor"

        return f"Matched {city_part.strip()} {search_title.lower()} contractor"

    def health_check(self) -> bool:
        """Check if this connector can operate.

        Returns:
            True if the connector is available.
        """
        return len(self._companies) > 0

    def validate_result(self, result: ConnectorResult) -> bool:
        """Validate a single connector result.

        Args:
            result: The connector result to validate.

        Returns:
            True if the result has valid company_name and website.
        """
        if not result.company_name or len(result.company_name.strip()) < 3:
            return False
        if not result.website:
            return False
        parsed = urlparse(result.website)
        return bool(parsed.netloc)


# ---------------------------------------------------------------------------
# Location Parsing Helpers
# ---------------------------------------------------------------------------

_STATE_MAP: dict[str, str] = {
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
    location = re.sub(r"\s+", " ", location.strip()).lower()
    state = "TX"
    city_loc = location

    # Find full state name
    for state_code, state_name in _STATE_MAP.items():
        pattern = rf"\b{re.escape(state_name.lower())}\b"
        if re.search(pattern, location):
            state = state_code
            city_loc = re.sub(pattern, "", location).strip()
            break

    # Find state code abbreviation
    if state == "TX":
        for state_code in _STATE_MAP:
            pattern = r"(?:^|[\s,;])" + state_code + r"(?:$|[\s,;])"
            if re.search(pattern, location, re.IGNORECASE):
                state = state_code
                city_loc = re.sub(pattern, " ", location, flags=re.IGNORECASE).strip()
                break

    # Clean up city
    for state_name in _STATE_MAP.values():
        city_loc = re.sub(r"\b" + re.escape(state_name.lower()) + r"\b", "", city_loc)

    city = re.sub(r"^[,\s;]+|[,\s;]+$", "", city_loc).strip() or None
    return city, state


# ---------------------------------------------------------------------------
# URL Validation Helpers
# ---------------------------------------------------------------------------


def _verify_and_clean_url(url: str) -> str | None:
    """Verify and clean a URL for use in ConnectorResult.

    Returns the cleaned URL if it appears valid, otherwise None.

    Args:
        url: Raw URL string.

    Returns:
        Cleaned URL or None if invalid.
    """
    if not url:
        return None

    cleaned = url.strip()
    if not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned

    try:
        parsed = urlparse(cleaned)
        if not parsed.netloc:
            return None
        if "." not in parsed.netloc:
            return None
        return cleaned
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Auto-registration on import
# ---------------------------------------------------------------------------
from app.connectors.connector_registry import ConnectorRegistry

ConnectorRegistry.register(TexasProcurementConnector())
