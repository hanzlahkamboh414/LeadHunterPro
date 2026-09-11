"""Phase G + I — Layer-1 dork-yield learning store.

The discovery layer learns, per plan-holder DORK TEMPLATE, whether the
companies it produced ever became a WORKING lead (a dossier that shows in the
leads list). After MIN_TRIALS dispatches with zero working leads the dork is
auto-dropped at dispatch time, so a future pass never spends a provider credit
on it.

Phase I segments the evidence by (trade | location): rows are keyed
``(template, segment)`` and ``should_skip`` applies the segment hierarchy
(global-drop authoritative; a segment self-decides only with its own trials).
"""

from __future__ import annotations

import sqlite3

from app.discovery.yield_learning import (
    MIN_TRIALS,
    DiscoveryYieldStore,
    segment_key,
)


# ---------------------------------------------------------------------------
# record_dispatch / record_working
# ---------------------------------------------------------------------------

def test_dispatch_accumulates_trials(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    store.record_dispatch("plan_holder:A")
    store.record_dispatch("plan_holder:A")
    assert store.get("plan_holder:A") == {"trials": 2, "working": 0}


def test_working_accumulates(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    store.record_working("plan_holder:A")
    store.record_working("plan_holder:A")
    assert store.get("plan_holder:A") == {"trials": 0, "working": 2}


def test_empty_key_is_noop(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    store.record_dispatch("")
    store.record_working("")
    assert store.all() == {}


def test_get_missing_is_none(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    assert store.get("nope") is None


def test_all_and_reset(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    store.record_dispatch("A")
    store.record_working("B")
    assert store.all() == {"A": {"trials": 1, "working": 0},
                           "B": {"trials": 0, "working": 1}}
    store.reset()
    assert store.all() == {}


# ---------------------------------------------------------------------------
# should_skip — the drop rule
# ---------------------------------------------------------------------------

def test_should_skip_keeps_until_min_trials(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(MIN_TRIALS - 1):
        store.record_dispatch("dork")
    assert store.should_skip("dork") is False


def test_should_skip_drops_zero_working_after_min_trials(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(MIN_TRIALS):
        store.record_dispatch("dork")
    assert store.should_skip("dork") is True


def test_should_skip_keeps_once_any_working(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(MIN_TRIALS * 2):
        store.record_dispatch("dork")
    store.record_working("dork")
    assert store.should_skip("dork") is False


def test_should_skip_unknown_and_empty(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    assert store.should_skip("never_run") is False
    assert store.should_skip("") is False


def test_delete_template_resurrects(tmp_path):
    """P-G — manual, logged override: deleting a dropped template clears its
    record so it dispatches again. Resurrection is a HUMAN decision only."""
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(MIN_TRIALS):
        store.record_dispatch("dork")
    assert store.should_skip("dork") is True
    store.delete_template("dork")
    assert store.get("dork") is None
    assert store.should_skip("dork") is False


# ---------------------------------------------------------------------------
# Phase I — segment_key (the stable (trade, location) identity)
# ---------------------------------------------------------------------------

def test_segment_key_normalizes_case_and_whitespace():
    assert segment_key("Roofing", "Dallas TX") == "roofing | dallas tx"
    assert segment_key("roofing", "Dallas   tx ") == "roofing | dallas tx"
    assert segment_key("  Roofing  ", "  ") == "roofing"


def test_segment_key_blank_halves_dropped():
    assert segment_key("", "") == ""
    assert segment_key("  ", "") == ""
    assert segment_key("roofing", "") == "roofing"
    assert segment_key("", "texas") == "texas"


# ---------------------------------------------------------------------------
# Phase I — per-segment rows stay independent
# ---------------------------------------------------------------------------

def test_segment_rows_independent(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    seg_a = segment_key("roofing", "dallas tx")
    seg_b = segment_key("roofing", "houston tx")
    store.record_dispatch("dork", seg_a)
    store.record_dispatch("dork", seg_a)
    store.record_working("dork", seg_b)
    # Same template, different segment -> distinct rows.
    assert store.get("dork", seg_a) == {"trials": 2, "working": 0}
    assert store.get("dork", seg_b) == {"trials": 0, "working": 1}
    # Global row is untouched by segment writes.
    assert store.get("dork") is None


def test_all_keys_carry_segment_suffix(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    store.record_dispatch("A")
    store.record_working("B", segment_key("gc", "houston tx"))
    keys = store.all()
    assert "A" in keys  # global key, no suffix
    assert f"B|{segment_key('gc', 'houston tx')}" in keys


# ---------------------------------------------------------------------------
# Phase I — segment-hierarchy should_skip
# ---------------------------------------------------------------------------

def test_global_drop_is_authoritative(tmp_path):
    """A global drop skips EVERY segment — one-way, even a segment with no
    self evidence (never costs another MIN_TRIALS to reconfirm)."""
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(MIN_TRIALS):
        store.record_dispatch("dork")  # GLOBAL dead
    assert store.should_skip("dork", segment_key("roofing", "dallas tx")) is True
    assert store.should_skip("dork", segment_key("gc", "houston tx")) is True


def test_segment_decides_at_own_min_trials(tmp_path):
    """Global keeps; a segment that reaches its OWN MIN_TRIALS zero-working
    drops for itself while the global row stays keep."""
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    store.record_working("dork")  # GLOBAL proves working -> global keep
    seg = segment_key("roofing", "dallas tx")
    for _ in range(MIN_TRIALS):
        store.record_dispatch("dork", seg)
    assert store.should_skip("dork") is False          # global keep
    assert store.should_skip("dork", seg) is True      # segment self-drop


def test_segment_falls_back_to_global_keep_before_own_trials(tmp_path):
    """A segment with few of its own dispatches falls back to the global row
    (default keep) — a quiet segment must not be starved by chance."""
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    seg = segment_key("roofing", "dallas tx")
    for _ in range(MIN_TRIALS - 1):
        store.record_dispatch("dork", seg)
    assert store.should_skip("dork", seg) is False


def test_segment_with_working_keeps(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    seg = segment_key("roofing", "dallas tx")
    for _ in range(MIN_TRIALS * 2):
        store.record_dispatch("dork", seg)
    store.record_working("dork", seg)
    assert store.should_skip("dork", seg) is False


def test_segment_delete_resurrects_segment_only(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    seg = segment_key("roofing", "dallas tx")
    for _ in range(MIN_TRIALS):
        store.record_dispatch("dork", seg)
    assert store.should_skip("dork", seg) is True
    store.delete_template("dork", seg)
    assert store.get("dork", seg) is None
    assert store.should_skip("dork", seg) is False


# ---------------------------------------------------------------------------
# Phase I — additive migration: legacy single-column-PK rows become GLOBAL
# ---------------------------------------------------------------------------

def test_migration_preserves_legacy_global_rows(tmp_path):
    db = str(tmp_path / "legacy.db")
    # Build a PRE-Phase-I table (template-only PK, no segment column) exactly
    # as the store created it.
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE discovery_template_yield ("
        "template TEXT NOT NULL, trials INTEGER NOT NULL DEFAULT 0, "
        "working INTEGER NOT NULL DEFAULT 0, "
        "last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (template))"
    )
    conn.execute(
        "INSERT INTO discovery_template_yield (template, trials, working) "
        "VALUES ('legacy:A', 5, 2), ('legacy:B', 14, 0)"
    )
    conn.commit()
    conn.close()

    store = DiscoveryYieldStore(db)  # triggers the additive migration
    # Every legacy row survives as the GLOBAL row (segment='').
    assert store.get("legacy:A") == {"trials": 5, "working": 2}
    assert store.get("legacy:B") == {"trials": 14, "working": 0}
    assert store.get("legacy:A", "") == {"trials": 5, "working": 2}
    # The widened PK + segment writes now work on the migrated table.
    store.record_dispatch("legacy:B", segment_key("roofing", "dallas tx"))
    assert store.get("legacy:B", segment_key("roofing", "dallas tx")) == {
        "trials": 1, "working": 0,
    }
    # Volume checks: the 12-trial legacy:B row is now a global drop.
    assert store.should_skip("legacy:B") is True
