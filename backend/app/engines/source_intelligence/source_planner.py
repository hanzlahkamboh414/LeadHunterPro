"""Source planning logic for the Construction Source Intelligence Engine.

Takes an industry + location query and returns a ranked list of
public sources most likely to yield construction-company leads.
No crawling is performed — only source selection and ranking.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

from app.engines.source_intelligence.source_models import (
    NATIONAL_SOURCES,
    TRADE_ASSOCIATIONS,
    LICENSING_BOARDS,
    NEWS_SOURCES,
    CHAMBER_PATTERNS,
    CrawlStrategy,
    SourcePlannerRequest,
    SourcePlannerResult,
    SourceRecord,
    SourceType,
)

logger = logging.getLogger(__name__)

# State abbreviation lookup (common US states).
_STATE_ABBR: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}


def _resolve_state(raw: str | None) -> str | None:
    """Normalize a state string to its two-letter abbreviation."""
    if not raw:
        return None
    key = raw.strip().lower()
    return _STATE_ABBR.get(key, raw.strip().upper()[:2] if len(raw.strip()) >= 2 else None)


def _match_trade_assoc(state_abbr: str | None) -> list[dict]:
    """Return trade-association entries matching the given state."""
    if not state_abbr:
        return []
    return TRADE_ASSOCIATIONS.get(state_abbr.upper(), [])


def _match_licensing_board(state_abbr: str | None) -> list[dict]:
    """Return licensing-board entries matching the given state."""
    if not state_abbr:
        return []
    return LICENSING_BOARDS.get(state_abbr.upper(), [])


def _build_chamber_sources(city: str | None, state_abbr: str | None) -> list[dict]:
    """Generate placeholder chamber-of-commerce entries for the location."""
    if not city:
        return []
    city_slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
    state_part = f"-{state_abbr.lower()}" if state_abbr else ""
    return [
        {
            "name": f"{city.title()} Chamber of Commerce",
            "source_type": "local_chamber",
            "state": state_abbr,
            "city": city,
            "priority": 8,
            "crawl_strategy": "html_scraper",
            "supports_company_discovery": True,
            "supports_contact": True,
            "url": f"https://www.{city_slug}{state_part}.com/chamber",
            "notes": f"Local business network for {city}{f', {state_abbr}' if state_abbr else ''}.",
        }
    ]


def _normalize_source(raw: dict, default_state: str | None = None) -> SourceRecord:
    """Convert a raw dict into a SourceRecord with defaults applied."""
    source_type_str = raw.get("source_type", "business_directory")
    valid_source_types = {
        "government", "trade_association", "industry_publication",
        "business_directory", "professional_network", "local_chamber",
        "licensing_board", "news_media",
    }
    if source_type_str not in valid_source_types:
        logger.warning("Unknown source_type %r, defaulting to business_directory", source_type_str)
        source_type_str = "business_directory"

    crawl_strategy_str = raw.get("crawl_strategy", "html_scraper")
    valid_crawl_strategies = {"html_scraper", "api_client", "rss_feed", "csv_download", "manual_review"}
    if crawl_strategy_str not in valid_crawl_strategies:
        logger.warning("Unknown crawl_strategy %r, defaulting to html_scraper", crawl_strategy_str)
        crawl_strategy_str = "html_scraper"

    return SourceRecord(
        name=raw["name"],
        source_type=source_type_str,
        country=raw.get("country", "USA"),
        state=raw.get("state") or default_state,
        city=raw.get("city"),
        priority=raw.get("priority", 10),
        crawl_strategy=crawl_strategy_str,
        supports_company_discovery=raw.get("supports_company_discovery", False),
        supports_bid_discovery=raw.get("supports_bid_discovery", False),
        supports_leadership=raw.get("supports_leadership", False),
        supports_contact=raw.get("supports_contact", False),
        url=raw.get("url", ""),
        notes=raw.get("notes", ""),
    )


class SourcePlanner:
    """Plans which public sources to target for a construction-lead query."""

    def plan(self, request: SourcePlannerRequest) -> SourcePlannerResult:
        """Return a ranked list of public sources for the given query.

        Args:
            request: Industry, country, state, city parameters.

        Returns:
            SourcePlannerResult with ranked sources and metadata.
        """
        logger.info(
            "Source planning: industry=%r country=%r state=%r city=%r",
            request.industry,
            request.country,
            request.state,
            request.city,
        )

        state_abbr = _resolve_state(request.state)
        all_entries: list[dict] = []

        # 1. Trade associations for the state (highest relevance).
        all_entries.extend(_match_trade_assoc(state_abbr))

        # 2. National directories (always useful).
        all_entries.extend(NATIONAL_SOURCES)

        # 3. State licensing boards.
        all_entries.extend(_match_licensing_board(state_abbr))

        # 4. Local chamber of commerce.
        all_entries.extend(_build_chamber_sources(request.city, state_abbr))

        # 5. Industry news (broader reach).
        all_entries.extend(NEWS_SOURCES)

        # 6. Duplicate-state associations when no state match found.
        if not state_abbr:
            logger.info("No state specified — adding national associations only")

        # Deduplicate by name (case-insensitive).
        seen_names: set[str] = set()
        unique: list[dict] = []
        for entry in all_entries:
            key = entry["name"].lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            unique.append(entry)

        # Normalize and sort by priority (lower = better).
        records: list[SourceRecord] = [
            _normalize_source(e, default_state=state_abbr)
            for e in unique
        ]
        records.sort(key=lambda r: r.priority)

        summary_parts = [f"industry={request.industry!r}"]
        if request.country:
            summary_parts.append(f"country={request.country!r}")
        if state_abbr:
            summary_parts.append(f"state={state_abbr!r}")
        if request.city:
            summary_parts.append(f"city={request.city!r}")

        result = SourcePlannerResult(
            sources=records,
            query_summary=" ".join(summary_parts),
            total_sources=len(records),
            skipped_sources=sum(
                1 for r in records if not r.supports_company_discovery
            ),
        )

        logger.info(
            "Planning complete: %d sources selected, %d skipped (no company-discovery support)",
            result.total_sources,
            result.skipped_sources,
        )
        return result
