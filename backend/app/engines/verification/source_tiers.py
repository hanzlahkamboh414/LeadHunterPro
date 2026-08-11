"""Source quality tiers for accuracy-first lead discovery.

Tiers (lower = more authoritative, higher = weaker / not for live use):

    Tier 1 — official company website, government records, licensing boards
    Tier 2 — trade associations, reputable / established business directories
    Tier 3 — search providers, secondary aggregators, unclassified sources
    Tier 4 — fixture / demo / test data (NEVER used in live/accuracy mode)

The tier is a *verification input*, not a verdict: a Tier-1 source raises
confidence, a Tier-4 source is excluded from live output entirely
(CLAUDE.md §1). ``is_live_tier`` is the gate that keeps Tier-4 data out of
live output.
"""

from __future__ import annotations

from enum import IntEnum


class SourceTier(IntEnum):
    """Quality tier for a discovery source."""

    OFFICIAL = 1  # official website / government / licensing
    TRUSTED = 2  # trade association / reputable directory
    SEARCH = 3  # search provider / aggregator / unclassified
    FIXTURE = 4  # fixture / demo / test — never live


TIER_LABELS: dict[SourceTier, str] = {
    SourceTier.OFFICIAL: "official website / government / licensing",
    SourceTier.TRUSTED: "trade association / reputable directory",
    SourceTier.SEARCH: "search provider / aggregator",
    SourceTier.FIXTURE: "fixture / demo / test",
}

# Map a source name OR a SourcePlanner source_type to a tier. ``source_name``
# is checked first (most specific), then ``source_type``.
_SOURCE_TYPE_TIERS: dict[str, SourceTier] = {
    # Tier 1 — official / government / licensing
    "official_website": SourceTier.OFFICIAL,
    "government": SourceTier.OFFICIAL,
    "licensing": SourceTier.OFFICIAL,
    "licensing_board": SourceTier.OFFICIAL,
    # Tier 2 — trade associations / reputable directories
    "trade_association": SourceTier.TRUSTED,
    "local_chamber": SourceTier.TRUSTED,
    "business_directory": SourceTier.TRUSTED,
    "reputable_directory": SourceTier.TRUSTED,
    # Tier 3 — search / aggregators / unclassified
    "search_provider": SourceTier.SEARCH,
    "search_providers": SourceTier.SEARCH,
    "aggregator": SourceTier.SEARCH,
    "industry_publication": SourceTier.SEARCH,
    "professional_network": SourceTier.SEARCH,
    "news_media": SourceTier.SEARCH,
    "directory_crawl": SourceTier.SEARCH,
    # Tier 4 — fixture / demo / test. ``texas_procurement`` currently labels
    # the ADR-002 fixture dataset; a live government connector of the same
    # name would need its own source_name mapped to Tier 1.
    "fixture_bridge": SourceTier.FIXTURE,
    "fixture": SourceTier.FIXTURE,
    "demo": SourceTier.FIXTURE,
    "test": SourceTier.FIXTURE,
    "texas_procurement": SourceTier.FIXTURE,
}

# Unknown sources are treated as secondary/aggregator — never as Tier 1/2.
_DEFAULT_TIER = SourceTier.SEARCH


def tier_for_source(
    source: str | None,
    source_type: str | None = None,
) -> SourceTier:
    """Return the quality tier for a source name and/or source type.

    ``source`` (e.g. the source's ``source_name``) wins over ``source_type``
    (e.g. a SourcePlanner ``source_type``). Unknown sources default to Tier 3.
    """
    for candidate in (source, source_type):
        if not candidate:
            continue
        tier = _SOURCE_TYPE_TIERS.get(str(candidate).strip().lower())
        if tier is not None:
            return tier
    return _DEFAULT_TIER


def is_live_tier(tier: int | SourceTier) -> bool:
    """True when a source tier is usable in live/accuracy mode.

    Tier 4 (fixture/demo/test) is never a live source.
    """
    return int(tier) < int(SourceTier.FIXTURE)


def is_fixture_source(
    source: str | None,
    source_type: str | None = None,
) -> bool:
    """True when the source maps to the Tier-4 fixture bridge."""
    return tier_for_source(source, source_type) == SourceTier.FIXTURE


def tier_label(tier: int | SourceTier) -> str:
    """Human-readable label for a tier (for reports/exports)."""
    return TIER_LABELS[SourceTier(int(tier))]
