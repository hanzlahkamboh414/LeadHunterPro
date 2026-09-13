"""P6 — HarvesterStore contracts: demand upserts, daily quotas, run
history (rotation memory), the staleness re-verify queue, and the
state_from_location helper the demand hooks parse locations with.
"""

from __future__ import annotations

from app.harvester.store import (
    HarvesterStore,
    state_from_location,
)


def _store(tmp_path):
    return HarvesterStore(db_path=str(tmp_path / "harvester.db"))


# ---------------------------------------------------------------------------
# state_from_location
# ---------------------------------------------------------------------------

def test_state_from_location_trailing_code():
    assert state_from_location("Dallas TX") == "TX"
    assert state_from_location("Vancouver, WA") == "WA"
    assert state_from_location("austin tx") == "TX"


def test_state_from_location_full_name():
    assert state_from_location("Texas") == "TX"
    assert state_from_location("Harris County, Texas") == "TX"
    assert state_from_location("washington") == "WA"


def test_state_from_location_honest_miss():
    """Unparsable = '' — a guessed state would silently mis-rank demand."""
    assert state_from_location("") == ""
    assert state_from_location("London") == ""
    assert state_from_location("Smith Jones") == ""


# ---------------------------------------------------------------------------
# demand
# ---------------------------------------------------------------------------

def test_record_demand_upserts_and_ranks(tmp_path):
    store = _store(tmp_path)
    store.record_demand("gc", "TX")
    store.record_demand("gc", "TX")
    store.record_demand("roofing", "WA")

    top = store.top_demand()
    assert top[0] == {"trade": "gc", "state": "TX", "weight": 2.0}
    assert top[1]["trade"] == "roofing"


def test_record_demand_normalizes_case(tmp_path):
    store = _store(tmp_path)
    store.record_demand("GC", "tx")
    store.record_demand("gc", "TX")
    assert store.top_demand()[0]["weight"] == 2.0


# ---------------------------------------------------------------------------
# daily quotas
# ---------------------------------------------------------------------------

def test_quota_room_counts_and_clamps(tmp_path):
    store = _store(tmp_path)
    assert store.quota_room("phones", 5000) == 5000
    store.record_stocked("phones", 4998)
    assert store.quota_room("phones", 5000) == 2
    store.record_stocked("phones", 10)
    assert store.quota_room("phones", 5000) == 0  # never negative
    # A different vertical is a different counter.
    assert store.quota_room("emails", 2000) == 2000


def test_quota_counters_are_per_day(tmp_path):
    store = _store(tmp_path)
    store.record_stocked("emails", 2000)
    assert store.quota_room("emails", 2000) == 0
    # Yesterday's rows do not count against today.
    assert store.quota_room("emails", 2000, day="2020-01-01") == 2000


# ---------------------------------------------------------------------------
# run history (rotation / backoff)
# ---------------------------------------------------------------------------

def test_last_run_at_and_recent_runs(tmp_path):
    store = _store(tmp_path)
    assert store.last_run_at("phones", "gc", "WA") == ""  # never harvested
    store.record_run("phones", "gc", "WA", "success", stocked=42)
    assert store.last_run_at("phones", "GC", "wa") != ""  # pair key is canonical

    runs = store.recent_runs("phones")
    assert len(runs) == 1
    assert runs[0]["outcome"] == "success"
    assert runs[0]["stocked"] == 42


# ---------------------------------------------------------------------------
# staleness re-verify queue
# ---------------------------------------------------------------------------

def test_enqueue_reverify_idempotent_until_processed(tmp_path):
    store = _store(tmp_path)
    store.enqueue_reverify("gc", "WA", "stale")
    store.enqueue_reverify("gc", "WA", "stale")  # same pending pair
    assert store.reverify_pending() == 1

    items = store.take_reverify()
    assert items == [{"id": 1, "trade": "gc", "state": "WA",
                      "reason": "stale"}]
    assert store.reverify_pending() == 0

    # After processing, the same pair may be queued again.
    store.enqueue_reverify("gc", "WA", "stale")
    assert store.reverify_pending() == 1


def test_take_reverify_pops_oldest_first(tmp_path):
    store = _store(tmp_path)
    store.enqueue_reverify("gc", "WA", "stale")
    store.enqueue_reverify("electrical", "TX", "stale")
    items = store.take_reverify(limit=1)
    assert items[0]["trade"] == "gc"
    assert store.take_reverify(limit=1)[0]["trade"] == "electrical"
