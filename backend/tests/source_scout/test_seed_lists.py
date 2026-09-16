"""Seed lists — committed-data invariants (coverage_engine_v2.md §2.3).

These run against the REAL committed CSVs, not fixtures: a broken seed
commit fails in CI, never on the VPS. The vocabulary is the same
CANONICAL_TRADES the demand system consumes (no second list, anywhere).
"""

from __future__ import annotations

import csv

import pytest

from app.discovery.tradefold import CANONICAL_TRADES
from app.source_scout.boards import (
    CBP_CSV,
    EMAIL_SOURCES_CSV,
    SCOPE_SPECIALS,
    STATE_BOARDS_CSV,
    SeedValidationError,
    load_email_sources,
    load_state_boards,
)


def _rows(path):
    with path.open("r", newline="", encoding="utf-8") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def test_board_seed_has_all_states_and_ranks():
    rows = load_state_boards()
    assert len(rows) >= 45
    states = [r["state"] for r in rows]
    assert len(set(states)) == len(states)
    ranks = [int(r["priority_rank"]) for r in rows]
    assert sorted(ranks) == list(range(1, len(rows) + 1))


def test_board_seed_trade_scope_in_vocabulary():
    rows = load_state_boards()
    for r in rows:
        tokens = [t for t in r["trade_scope"].split(",") if t.strip()]
        for t in tokens:
            assert t in CANONICAL_TRADES or t in SCOPE_SPECIALS, \
                f"{r['state']}: {t!r} not in shared vocabulary"


def test_board_seed_ranks_match_cbp_raw():
    """priority_rank + estab mirror cbp_construction_2023.csv exactly —
    a drift between the two committed files is a broken commit."""
    boards = load_state_boards()
    cbp = {r["state"]: r for r in _rows(CBP_CSV)}
    assert len(cbp) == len(boards)
    for r in boards:
        assert int(r["priority_rank"]) == int(cbp[r["state"]]["priority_rank"])
        assert int(r["estab"]) == int(cbp[r["state"]]["estab"])


def test_board_seed_known_access_is_prober_path():
    for r in load_state_boards():
        assert r["known_access"] in (
            "bulk_file", "open_data_api", "xhr_json", "html_form", "pdf",
            "soda", "socrata",
        ) or r["known_access"] == ""


def test_bad_trade_scope_token_rejected(tmp_path):
    import shutil
    bad = tmp_path / "bad.csv"
    shutil.copy(STATE_BOARDS_CSV, bad)
    text = bad.read_text(encoding="utf-8")
    # MN's legal scope becomes a made-up trade — must NOT pass validation
    text = text.replace("gc,electrical,mechanical,plumbing", "marblemason")
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(SeedValidationError, match="marblemason"):
        load_state_boards(bad)


def test_email_seed_named_rows_with_urls():
    rows = load_email_sources()
    assert len(rows) >= 2
    sids = {r["source_id"] for r in rows}
    assert "overture_places" in sids and "common_crawl" in sids
    for r in rows:
        assert r["base_url"].startswith("https://")
        assert r["estimated_rows"].strip()
        assert r["access_path"] in (
            "bulk_file", "open_data_api", "xhr_json", "html_form", "pdf",
            "soda", "socrata",
        )


def test_email_seed_must_claim_email_present(tmp_path):
    import shutil
    bad = tmp_path / "bad_emails.csv"
    shutil.copy(EMAIL_SOURCES_CSV, bad)
    bad.write_text(
        bad.read_text(encoding="utf-8")
        .replace('"email"', '"phone"'), encoding="utf-8")
    with pytest.raises(SeedValidationError, match="email present"):
        load_email_sources(bad)