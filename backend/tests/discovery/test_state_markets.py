"""Tests for the state-level geo fallback (Phase 1 demand fix).

Live proof this guards (2026-09-12): the Honolulu HI and Wichita KS runs
stopped at 1-2 working leads with ``no_progress_plateau`` because
``_expandable_markets()`` drew ONLY from the six-metro fallback table and
returned [] for every other US metro.
"""

from __future__ import annotations

from app.discovery.query_expansion import _METRO_FALLBACK, _fold, metro_fallback
from app.discovery.state_markets import (
    STATE_METROS,
    parse_state_code,
    state_markets,
)


class TestParseStateCode:
    def test_code_form(self):
        assert parse_state_code("Wichita, KS") == "KS"
        assert parse_state_code("Honolulu HI") == "HI"

    def test_full_name_form(self):
        assert parse_state_code("Des Moines Iowa") == "IA"
        assert parse_state_code("Sedgwick County Kansas") == "KS"

    def test_code_wins_over_name(self):
        # "Washington DC" is the District, not Washington state — the valid
        # 2-letter code must take precedence over the state name.
        assert parse_state_code("Washington DC") == "DC"

    def test_statewide_literals(self):
        assert parse_state_code("Texas") == "TX"
        assert parse_state_code("TX") == "TX"

    def test_non_us_and_garbage(self):
        assert parse_state_code("London UK") == ""  # UK is not a US code
        assert parse_state_code("") == ""
        assert parse_state_code("Somewhere") == ""


class TestStateMarkets:
    def test_unknown_metro_gets_state_expansion(self):
        """The Honolulu live failure: an unknown metro must NOT return an
        empty expansion list — the state's other metros + statewide are
        the path forward."""
        out = state_markets("Honolulu, HI")
        assert "Pearl City, HI" in out
        assert "Hilo, HI" in out
        assert out[-1] == "Hawaii"  # statewide is the broadest net, LAST

    def test_literal_metro_excluded(self):
        out = state_markets("Wichita, KS")
        assert "Wichita, KS" not in out
        assert "Overland Park, KS" in out
        assert "Kansas" in out

    def test_county_literal_still_expands(self):
        """A county-level literal (AI produced 'Sedgwick County Kansas' in
        the live Wichita run) is NOT statewide — it gets the state tier."""
        out = state_markets("Sedgwick County Kansas")
        assert "Wichita, KS" in out
        assert out[-1] == "Kansas"

    def test_statewide_literal_returns_empty(self):
        """'Texas' already covers every TX metro — expanding to a subset
        would NARROW the surface. The honest answer is nothing to add."""
        assert state_markets("Texas") == []
        assert state_markets("TX") == []

    def test_non_us_returns_empty(self):
        assert state_markets("London") == []
        assert state_markets("") == []

    def test_entries_are_valid_markets(self):
        """Every STATE_METROS entry is a real 'City, ST' market for its own
        state key — the rotation would otherwise search garbage."""
        for code, metros in STATE_METROS.items():
            for m in metros:
                assert parse_state_code(m) == code, f"{m!r} is not in {code}"


class TestMetroFallback:
    def test_known_metro_tier1_first_then_state_tier(self):
        """Known metro: hand-curated counties/suburbs come FIRST (highest
        precision), then the state's other metros, statewide LAST."""
        out = metro_fallback("Dallas TX")
        assert out[0] == "Dallas County TX"  # tier 1 leads
        assert "Houston, TX" in out  # tier 2: other TX metros
        assert out[-1] == "Texas"  # statewide is the last resort

    def test_unknown_metro_gets_state_tier(self):
        out = metro_fallback("Honolulu, HI")
        assert out  # never empty for a US metro
        assert "Pearl City, HI" in out

    def test_never_contains_the_literal(self):
        for loc in ["Dallas TX", "Honolulu HI", "Wichita KS", "Cedar Rapids IA"]:
            assert all(_fold(m) != _fold(loc) for m in metro_fallback(loc))

    def test_no_fold_duplicates(self):
        for loc in ["Dallas TX", "Honolulu HI", "Wichita KS"]:
            keys = [_fold(m) for m in metro_fallback(loc)]
            assert len(keys) == len(set(keys)), f"duplicate market in {loc!r}"

    def test_statewide_literal_stays_empty(self):
        """A literal that IS a state has nothing broader to expand to."""
        assert metro_fallback("Texas") == []
        assert metro_fallback("TX") == []

    def test_metro_table_entries_still_reachable(self):
        """Tier 1 is unchanged — every _METRO_FALLBACK entry still appears
        (the six known metros keep their higher-precision expansion)."""
        for loc, markets in _METRO_FALLBACK.items():
            pretty = ", ".join(part.title() for part in loc.split())
            out = metro_fallback(pretty)
            for m in markets:
                assert _fold(m) in {_fold(x) for x in out}, f"{m!r} missing"
