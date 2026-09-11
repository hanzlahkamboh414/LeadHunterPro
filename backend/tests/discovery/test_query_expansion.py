"""Phase J — AI query-surface expansion (unit tests, fully offline)."""

import pytest

from app.discovery.query_expansion import (
    _MAX_LOCATION,
    _MAX_TRADE,
    _METRO_FALLBACK,
    _fold,
    _parse,
    _sanitize_list,
    generate_query_expansion,
    merge_locations,
)


def test_sanitize_drops_junk_dedupes_and_caps():
    clean = _sanitize_list(
        [
            "General Contractor",
            "Builders",
            'site:evil.com contractor',      # search-operator -> rejected
            "x" * 99,                         # oversize -> rejected
            "General Contractors",            # duplicate of base -> dropped
            "filetype:pdf GC",                # operator -> rejected
            "Construction Group",
        ],
        base="General Contractors",
        cap=6,
    )
    assert clean == ["General Contractor", "Builders", "Construction Group"]


def test_sanitize_respects_cap():
    clean = _sanitize_list(["a1", "a2", "a3", "a4", "a5"], base="base", cap=3)
    assert clean == ["a1", "a2", "a3"]


def test_parse_sections():
    trades, locs = _parse(
        'TRADE:\n"Building Contractors"\n"GC"\n'
        'LOCATION:\n"Harris County TX"\n"San Antonio, TX"'
    )
    assert trades == ["Building Contractors", "GC"]
    assert locs == ["Harris County TX", "San Antonio, TX"]


def test_parse_fallback_when_no_section_markers():
    trades, locs = _parse('"Commercial Builders" "New Build GC"')
    assert trades == ["Commercial Builders", "New Build GC"]
    assert locs == []


def test_parse_empty_reply():
    assert _parse("") == ([], [])


def test_generate_with_fake_ask_expands_both_axes():
    def fake_ask(_prompt: str) -> str:
        return (
            'TRADE:\n"Building GC"\n"Commercial Builders"\n'
            'LOCATION:\n"Harris County TX"\n"Fort Bend County TX"'
        )

    out = generate_query_expansion("General Contractors", "Houston TX", ai_ask=fake_ask)
    assert out["trade_variants"] == ["Building GC", "Commercial Builders"]
    # Surface Expansion Guard (Inc 1): 2 distinct markets is a THIN surface —
    # the deterministic metro fallback merges IN (union, not either/or). The
    # AI's own picks lead, the fallback fills the breadth behind them.
    assert out["location_variants"] == [
        "Harris County TX", "Fort Bend County TX",
        "Montgomery County TX", "Galveston County TX",
        "Katy TX", "Sugar Land TX", "Conroe TX",
    ]
    assert "metro fallback merged" in out["reason"]
    assert out["retried"] is False
    assert len(out["raw_replies"]) == 1  # rich reply, no retry needed


def test_generate_garbage_reply_returns_honest_empty_with_reason():
    """Unknown metro + garbage reply: no deterministic fallback exists, so an
    empty stays an empty — but loud (reason) after the free retry was burned."""
    out = generate_query_expansion("General Contractors", "Butte MT",
                                   ai_ask=lambda _p: "no idea what you want")
    assert out["retried"] is True  # the one retry was spent, honestly reported
    assert len(out["raw_replies"]) == 2
    assert out["trade_variants"] == []
    assert out["location_variants"] == []
    assert out["reason"]  # loud, not silent


def test_retry_fires_when_first_call_yields_one_distinct_location():
    """Phase K2: a single weak flash-model call (the 04:06 starvation shape)
    gets ONE re-ask. The retry's reply must survive into the final surface."""
    calls = {"n": 0}

    def fake_ask(_prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return 'LOCATION:\n"Harris County TX"'  # ONE market only
        return 'LOCATION:\n"Harris County TX"\n"Fort Bend County TX"\n"Montgomery County TX"'

    out = generate_query_expansion("GC", "Houston TX", ai_ask=fake_ask)
    assert calls["n"] == 2
    assert out["retried"] is True
    # Surface Expansion Guard (Inc 1): even after the retry, 3 distinct markets
    # is below the breadth bar — the fallback entries merge in behind the AI's.
    assert out["location_variants"] == [
        "Harris County TX", "Fort Bend County TX", "Montgomery County TX",
        "Galveston County TX", "Katy TX", "Sugar Land TX", "Conroe TX",
    ]
    assert "metro fallback merged" in out["reason"]
    assert len(out["raw_replies"]) == 2  # both raw replies diagnosable


def test_no_retry_when_first_call_yields_two_distinct_locations():
    calls = {"n": 0}

    def fake_ask(_prompt: str) -> str:
        calls["n"] += 1
        return 'LOCATION:\n"Harris County TX"\n"Fort Bend County TX"'

    out = generate_query_expansion("GC", "Houston TX", ai_ask=fake_ask)
    assert calls["n"] == 1
    assert out["retried"] is False
    assert len(out["raw_replies"]) == 1


def test_retry_then_metro_fallback_fills_breadth():
    """Retry still weak -> the deterministic metro map fills the geo surface,
    so a run keeps ADVANCING instead of grinding the literal market."""
    def fake_ask(_prompt: str) -> str:
        return 'LOCATION:\n"Bexar County TX"'  # one market, forever

    out = generate_query_expansion("General Contractors", "San Antonio TX", ai_ask=fake_ask)
    assert out["retried"] is True
    assert "Bexar County TX" in out["location_variants"]  # AI's answer kept
    assert "New Braunfels TX" in out["location_variants"]  # fail-safe breadth
    assert "Comal County TX" in out["location_variants"]
    assert out["reason"]  # the fallback is LOUD, never silent (§6)


def test_garbage_reply_still_gets_fallback_for_known_metro():
    """Even a fully-unusable reply cannot starve a known metro: the fallback
    fills; the reason says the AI returned nothing usable."""
    out = generate_query_expansion("General Contractors", "San Antonio TX",
                                   ai_ask=lambda _p: "no idea what you want")
    assert out["retried"] is True
    assert "New Braunfels TX" in out["location_variants"]
    assert "fallback" in out["reason"]


def test_metro_fallback_entries_are_never_the_literal():
    """Invariant: fallback entries are SEPARABLE markets, never re-spellings of
    the key metro — otherwise the rotation would narrow instead of broaden."""
    for loc, markets in _METRO_FALLBACK.items():
        assert markets, f"fallback for {loc!r} must not be empty"
        assert len(set(_fold(m) for m in markets)) == len(markets), f"{loc!r}"
        assert all(_fold(m) != loc for m in markets), f"{loc!r} repeats its key"


def test_generate_ai_exception_is_honest_not_fatal():
    def boom(_prompt: str) -> str:
        raise RuntimeError("provider down")

    out = generate_query_expansion("Roofing", "Dallas TX", ai_ask=boom)
    assert out["trade_variants"] == []
    assert "provider down" in out["reason"]


def test_caps_support_metro_breadth():
    """Phase K discovery-breadth: the caps are high enough to hold a whole metro
    geo surface (the 'San Antonio TX' run collapsed to ONE county because the old
    cap clipped breadth to a couple of locations)."""
    assert _MAX_TRADE >= 8
    assert _MAX_LOCATION >= 8


def test_merge_locations_keeps_literal_first():
    """The user's OWN market is ALWAYS searched — and searched FIRST — even when
    the AI expands it (the pipeline bug that made 'San Antonio TX' search only
    'Bexar County TX' and never the city)."""
    merged = merge_locations("San Antonio TX", ["New Braunfels TX", "Comal County TX"])
    assert merged == ["San Antonio TX", "New Braunfels TX", "Comal County TX"]


def test_merge_locations_folds_variants_that_are_the_literal():
    merged = merge_locations("San Antonio TX", ["Bexar County TX", "San Antonio TX", "Seguin TX"])
    assert merged == ["San Antonio TX", "Bexar County TX", "Seguin TX"]


def test_merge_locations_narrows_to_literal_when_ai_empty():
    """No AI variants -> the literal alone is the surface; breadth never empties."""
    assert merge_locations("San Antonio TX", []) == ["San Antonio TX"]


def test_merge_locations_drops_blanks_and_empty_literal():
    assert merge_locations("", ["A TX", "  ", "B TX"]) == ["A TX", "B TX"]
    assert merge_locations("", []) == []


def test_merge_locations_case_whitespace_folding():
    assert merge_locations("san antonio tx", [" San Antonio TX ", "Comal TX"]) == [
        "san antonio tx",
        "Comal TX",
    ]


def test_merge_locations_drops_garbled_county_near_duplicates():
    """PROOF (garbled-location guard): an AI-proposed 'Forum Bend County TX'
    — a garble of the already-accepted 'Fort Bend County TX' — must be dropped,
    not searched (it wastes a discovery pass on a query that never hits)."""
    literal = "Houston TX"
    ai = [
        "Harris County TX",
        "Fort Bend County TX",
        "Forum Bend County TX",      # garbled near-dup of Fort Bend -> dropped
        "Brazoria County TX",
        "Galveston County TX",
    ]
    assert merge_locations(literal, ai) == [
        "Houston TX",
        "Harris County TX",
        "Fort Bend County TX",
        "Brazoria County TX",
        "Galveston County TX",
    ]


def test_merge_locations_keeps_genuinely_distinct_markets():
    """Near-duplicate guard must never fold away REAL breadth — distinct
    cities/counties with only a shared stop-word stay separate."""
    literal = "Houston TX"
    ai = ["Harris County TX", "Fort Bend County TX", "Fort Worth TX"]
    assert merge_locations(literal, ai) == [
        "Houston TX",
        "Harris County TX",
        "Fort Bend County TX",
        "Fort Worth TX",
    ]