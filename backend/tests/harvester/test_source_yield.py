"""P10 — source-level yield learning: HarvesterStore contracts (hermetic).

The Phase G/I dork-learning pattern mirrored onto the phones lane: a
(source, segment) row accumulates trials (successful fetches) and working
credits (stocked NEW rows); enough zero-working trials drop the pair until
the drop re-arms; the global row stays authoritative; resurrection is
human-only. These tests pin the store's counting and gating rules.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.harvester.store import (
    SOURCE_MIN_TRIALS,
    HarvesterStore,
    source_segment,
)

SEG = source_segment("gc", "WA")


def _store(tmp_path) -> HarvesterStore:
    return HarvesterStore(db_path=str(tmp_path / "harvester.db"))


def _backdate_last_seen(tmp_path, days: int) -> None:
    """Age the trial history out of the re-arm window (a direct backdate is
    the honest way to cross a 30-day window in a test)."""
    old = (datetime.now(timezone.utc) - timedelta(days=days)) \
        .strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(str(tmp_path / "harvester.db"))
    conn.execute(
        "UPDATE source_yield SET last_seen = ? WHERE source_id = 'wa_license'",
        (old,),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# segment key + counting
# ---------------------------------------------------------------------------

def test_source_segment_key_shape(tmp_path):
    assert source_segment("Roofing", "wa") == "roofing | WA"
    assert source_segment("GC", "") == "gc"
    assert source_segment("", "TX") == "TX"
    assert source_segment("  dry  wall ", "tx") == "dry wall | TX"


def test_dispatch_and_working_count(tmp_path):
    store = _store(tmp_path)
    for _ in range(3):
        store.record_source_dispatch("wa_license", SEG)
    store.record_source_working("wa_license", SEG)
    row = store.source_yield_get("wa_license", SEG)
    assert row["trials"] == 3 and row["working"] == 1


def test_working_upserts_without_dispatch_row(tmp_path):
    """A working stock is never lost for want of a trial row (Phase G/I)."""
    store = _store(tmp_path)
    store.record_source_working("wa_license", SEG)
    row = store.source_yield_get("wa_license", SEG)
    assert row["trials"] == 0 and row["working"] == 1


def test_empty_source_id_is_ignored(tmp_path):
    store = _store(tmp_path)
    store.record_source_dispatch("", SEG)
    store.record_source_working("", SEG)
    assert store.source_yield_get("", SEG) is None
    assert store.source_should_skip("", SEG) is False


# ---------------------------------------------------------------------------
# gating — the Phase F/G/I hierarchy, time-bounded by the re-arm window
# ---------------------------------------------------------------------------

def test_keep_default_below_min_trials(tmp_path):
    store = _store(tmp_path)
    for _ in range(SOURCE_MIN_TRIALS - 1):
        store.record_source_dispatch("wa_license", SEG)
    assert store.source_should_skip("wa_license", SEG) is False


def test_zero_yield_drops_after_min_trials(tmp_path):
    store = _store(tmp_path)
    for _ in range(SOURCE_MIN_TRIALS):
        store.record_source_dispatch("wa_license", SEG)
    assert store.source_should_skip("wa_license", SEG) is True


def test_working_row_never_drops(tmp_path):
    """The 0-only rule (deliberately NOT the 40-trial proportional prune —
    mirrors the discovery yield_learning decision)."""
    store = _store(tmp_path)
    for _ in range(SOURCE_MIN_TRIALS + 5):
        store.record_source_dispatch("wa_license", SEG)
    store.record_source_working("wa_license", SEG)
    assert store.source_should_skip("wa_license", SEG) is False


def test_segment_row_decides_only_for_its_segment(tmp_path):
    store = _store(tmp_path)
    for _ in range(SOURCE_MIN_TRIALS):
        store.record_source_dispatch("wa_license", SEG)
    # the sibling segment has no data of its own -> global KEEP default
    assert store.source_should_skip(
        "wa_license", source_segment("roofing", "WA")) is False
    # and the global row itself was never written (Phase I behavior:
    # dispatches land on the segment row only)
    assert store.source_yield_get("wa_license", "") is None


def test_global_drop_is_authoritative(tmp_path):
    """A legacy/aggregate global drop wins even over a segment's own working
    evidence — the one-way Phase F rule."""
    store = _store(tmp_path)
    for _ in range(SOURCE_MIN_TRIALS):
        store.record_source_dispatch("wa_license", "")
    store.record_source_dispatch("wa_license", SEG)
    store.record_source_working("wa_license", SEG)
    assert store.source_should_skip("wa_license", SEG) is True


def test_drop_expires_after_rearm_window(tmp_path):
    """License boards issue NEW licenses weekly — a permanent drop would
    freeze a pair at its first saturation. Past the window, one exploratory
    trial is allowed again."""
    store = _store(tmp_path)
    for _ in range(SOURCE_MIN_TRIALS):
        store.record_source_dispatch("wa_license", SEG)
    assert store.source_should_skip("wa_license", SEG) is True
    _backdate_last_seen(tmp_path, days=31)
    assert store.source_should_skip("wa_license", SEG, rearm_days=30) is False


def test_human_resurrection_via_delete(tmp_path):
    store = _store(tmp_path)
    for _ in range(SOURCE_MIN_TRIALS):
        store.record_source_dispatch("wa_license", SEG)
    store.delete_source_yield("wa_license", SEG)
    assert store.source_yield_get("wa_license", SEG) is None
    assert store.source_should_skip("wa_license", SEG) is False


# ---------------------------------------------------------------------------
# run-history attribution (P10: harvest_runs gains the source column)
# ---------------------------------------------------------------------------

def test_record_run_stores_source(tmp_path):
    store = _store(tmp_path)
    store.record_run("phones", "gc", "WA", "success", 5, detail="x",
                     source="wa_license")
    store.record_run("phones", "electrical", "TX", "source_error", 0)
    runs = store.recent_runs("phones")
    assert runs[0]["source"] == ""  # legacy callers stay honest ('' = unknown)
    assert runs[1]["source"] == "wa_license"


def test_source_yield_all_shape(tmp_path):
    store = _store(tmp_path)
    store.record_source_dispatch("wa_license", SEG)
    store.record_source_working("tdlr_license", "")
    out = store.source_yield_all()
    assert out[f"wa_license | {SEG}"]["trials"] == 1
    assert out["tdlr_license"]["working"] == 1
