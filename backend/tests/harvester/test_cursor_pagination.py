"""Offset paging: the cursor that makes repeated runs worth anything.

Before this, every run of a (trade, state) pair asked the source for the
same first page, so the pair stocked once and reported 100% duplicates
forever (measured live 2026-09-23: WA gc had 55,184 matching rows and the
lane had only ever read the first 250). These contracts pin the walk
forward, the honest end-of-sweep signal, and the fact that a failed fetch
must never consume a window it never read.
"""

from __future__ import annotations

import logging

import pytest

from app.harvester.store import HarvesterStore
from app.harvester.worker import HarvesterWorker
from app.phones.soda import (
    TDLR_TRADE_VALUES,
    WA_DATASET,
    WA_TRADE_VALUES,
    SourceStatus,
    _page_params,
    fetch_license_records,
)
from app.phones.store import PhoneLeadsStore

PAIR = ("drywall", "WA")
SOURCE = "wa_license"
#: `drywall` is a real WA slug, so the pair resolves to a fetchable source
#: without inventing coverage.
WA_SLUG = PAIR[0]


def _store(tmp_path):
    return HarvesterStore(db_path=str(tmp_path / "harvester.db"))


def _record(slug: str, source_id: str, seq: int) -> dict:
    """One fetchable record, unique per seq so re-reading a window yields
    duplicates exactly like a real board does."""
    values = (WA_TRADE_VALUES if source_id == "wa_license"
              else TDLR_TRADE_VALUES)
    return {
        "phone": f"503957{seq:04d}",
        "person_name": "SMITH, JANE",
        "business_name": "Acme",
        "trade_category": values[slug][0],
        "city": "VANCOUVER",
        "state": "WA",
        "source": source_id,
        "license_status": "ACTIVE",
        "source_url": "https://example.gov/x",
    }


class PagedFetch:
    """Serves one scripted page per call.

    ``rows_per_call`` is what the SOURCE claimed to have in the window;
    ``valid_per_call`` (defaulting to the same) is how many usable records
    came back — a board legitimately returns rows we cannot stock, so the
    advance must ride on the source's row count, not on ours. The last
    entry repeats once the list runs out.
    """

    def __init__(self, rows_per_call, valid_per_call=None, fail=False):
        self.rows_per_call = list(rows_per_call)
        self.valid_per_call = (None if valid_per_call is None
                               else list(valid_per_call))
        self.fail = fail
        self.calls: list[dict] = []

    def __call__(self, source_id, slug, city, limit, offset=0):
        self.calls.append({"source_id": source_id, "slug": slug, "city": city,
                           "limit": limit, "offset": offset})
        if self.fail:
            return SourceStatus.UNAVAILABLE, [], {"error": "source down"}
        i = len(self.calls) - 1
        rows = self.rows_per_call[min(i, len(self.rows_per_call) - 1)]
        valid = rows
        if self.valid_per_call:
            valid = self.valid_per_call[min(i, len(self.valid_per_call) - 1)]
        records = [_record(slug, source_id, offset + k) for k in range(valid)]
        return SourceStatus.SUCCESS, records, {"rows_fetched": rows}


class _NoResearch:
    def __call__(self, query):  # pragma: no cover — never entered here
        raise AssertionError("the emails lane must not run")


def _worker(tmp_path, fetch, **kw):
    defaults = dict(
        fetch=fetch,
        research=_NoResearch(),
        active_jobs=lambda: 0,
        enabled=True,
        max_active=4,
        interval_s=0.01,
        phone_batch=250,
        email_batch=25,
        pair_cooldown_s=21600.0,
        min_pool_floor=100,
        staleness_days=30,
        daily_phone_quota=5000,
        daily_email_quota=2000,
    )
    defaults.update(kw)
    return HarvesterWorker(
        _store(tmp_path), PhoneLeadsStore(db_path=str(tmp_path / "phones.db")),
        lead_store=object(), pending_store=object(), **defaults,
    )


# ---------------------------------------------------------------------------
# the store: the cursor is durable, per-pair state
# ---------------------------------------------------------------------------

def test_an_unread_pair_starts_at_the_top(tmp_path):
    assert _store(tmp_path).cursor_get(SOURCE, *PAIR) == 0


def test_the_cursor_round_trips_and_is_per_pair(tmp_path):
    store = _store(tmp_path)
    store.cursor_advance(SOURCE, "drywall", "WA", 250)
    assert store.cursor_get(SOURCE, "drywall", "WA") == 250
    # A different pair is untouched — the walk is per (source, trade, state).
    assert store.cursor_get(SOURCE, "gc", "WA") == 0
    assert store.cursor_get("tdlr_license", "drywall", "WA") == 0


def test_advancing_moves_the_same_row_instead_of_piling_up(tmp_path):
    store = _store(tmp_path)
    store.cursor_advance(SOURCE, *PAIR, 250)
    store.cursor_advance(SOURCE, *PAIR, 500)
    store.cursor_advance(SOURCE, *PAIR, 750)
    assert store.cursor_get(SOURCE, *PAIR) == 750
    # One row per pair, so a long walk never grows the table.
    with store._conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM source_cursor").fetchone()[0]
    assert n == 1


def test_a_sweep_wrap_returns_to_zero_and_is_counted(tmp_path):
    store = _store(tmp_path)
    store.cursor_advance(SOURCE, *PAIR, 250)
    store.cursor_advance(SOURCE, *PAIR, 0, swept=True)
    assert store.cursor_get(SOURCE, *PAIR) == 0
    with store._conn() as conn:
        sweeps = conn.execute(
            "SELECT sweeps FROM source_cursor").fetchone()[0]
    assert sweeps == 1


def test_a_negative_offset_can_never_be_stored(tmp_path):
    store = _store(tmp_path)
    store.cursor_advance(SOURCE, *PAIR, -40)
    assert store.cursor_get(SOURCE, *PAIR) == 0


def test_an_unknown_source_is_never_stored(tmp_path):
    store = _store(tmp_path)
    store.cursor_advance("", *PAIR, 250)
    assert store.cursor_get("", *PAIR) == 0
    with store._conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM source_cursor").fetchone()[0]
    assert n == 0


# ---------------------------------------------------------------------------
# the walk: consecutive runs read NEW windows
# ---------------------------------------------------------------------------

def test_consecutive_runs_walk_forward_page_by_page(tmp_path):
    """The whole point: run two must not re-read run one's rows."""
    fetch = PagedFetch([250, 250, 250])
    w = _worker(tmp_path, fetch)
    for _ in range(3):
        w._harvest_phone_pair(*PAIR)
    assert [c["offset"] for c in fetch.calls] == [0, 250, 500]


def test_every_walked_page_stocks_new_rows(tmp_path):
    fetch = PagedFetch([250, 250])
    w = _worker(tmp_path, fetch)
    first = w._harvest_phone_pair(*PAIR)
    second = w._harvest_phone_pair(*PAIR)
    assert first["stocked"] == 250
    assert second["stocked"] == 250        # not 0 — the old bug's signature
    assert w._phone_store.unclaimed_count(*PAIR) == 500


def test_a_short_page_is_the_end_of_the_set_and_wraps(tmp_path):
    fetch = PagedFetch([250, 40, 250])
    w = _worker(tmp_path, fetch)
    w._harvest_phone_pair(*PAIR)           # offset 0   -> full page
    w._harvest_phone_pair(*PAIR)           # offset 250 -> 40 rows: the end
    assert w._store.cursor_get(SOURCE, *PAIR) == 0     # wrapped
    w._harvest_phone_pair(*PAIR)           # fresh pass, walks on again
    assert [c["offset"] for c in fetch.calls] == [0, 250, 0]
    assert w._store.cursor_get(SOURCE, *PAIR) == 250


def test_an_empty_page_also_wraps(tmp_path):
    """A pair whose set shrank (or whose filter now matches nothing) must
    not park the cursor past the end for good."""
    fetch = PagedFetch([0])
    w = _worker(tmp_path, fetch)
    w._harvest_phone_pair(*PAIR)
    w._harvest_phone_pair(*PAIR)
    assert [c["offset"] for c in fetch.calls] == [0, 0]


def test_the_source_row_count_governs_the_advance(tmp_path):
    """Rows the store cannot use are still rows the window consumed."""
    fetch = PagedFetch([250], valid_per_call=[3])
    w = _worker(tmp_path, fetch)
    w._harvest_phone_pair(*PAIR)
    assert w._store.cursor_get(SOURCE, *PAIR) == 250


def test_a_small_quota_room_asks_for_a_smaller_page_and_still_advances(tmp_path):
    fetch = PagedFetch([40])
    w = _worker(tmp_path, fetch, phone_batch=250, daily_phone_quota=40)
    w._harvest_phone_pair(*PAIR)
    assert fetch.calls[0]["limit"] == 40
    # 40 asked, 40 returned — that is NOT proof the set ended, so the walk
    # continues from 40 rather than wrapping.
    assert w._store.cursor_get(SOURCE, *PAIR) == 40


def test_a_failed_fetch_never_consumes_the_window(tmp_path):
    fetch = PagedFetch([250], fail=True)
    w = _worker(tmp_path, fetch)
    out = w._harvest_phone_pair(*PAIR)
    assert out["skipped"] == "source_error"
    assert w._store.cursor_get(SOURCE, *PAIR) == 0


def test_a_failed_pool_write_never_consumes_the_window(tmp_path):
    w = _worker(tmp_path, PagedFetch([250]))

    def fail_write(records):
        raise OSError("database unavailable")

    w._phone_store.add = fail_write
    with pytest.raises(OSError, match="database unavailable"):
        w._harvest_phone_pair(*PAIR)
    assert w._store.cursor_get(SOURCE, *PAIR) == 0


def test_the_cursor_is_durable_across_worker_instances(tmp_path):
    """Not in-memory: a restart must resume the walk, not restart it."""
    w = _worker(tmp_path, PagedFetch([250]))
    w._harvest_phone_pair(*PAIR)
    assert w._store.cursor_get(SOURCE, *PAIR) == 250

    fresh = _worker(tmp_path, PagedFetch([250, 250]))
    fresh._harvest_phone_pair(*PAIR)
    assert fresh._store.cursor_get(SOURCE, *PAIR) == 500
    assert fresh._fetch.calls[0]["offset"] == 250      # resumed, not reset


def test_the_run_detail_carries_the_offset(tmp_path):
    w = _worker(tmp_path, PagedFetch([250, 250]))
    w._harvest_phone_pair(*PAIR)
    w._harvest_phone_pair(*PAIR)
    runs = [r for r in w._store.recent_runs("phones", 10)
            if r["trade"] == PAIR[0] and r["state"] == PAIR[1]]
    assert "offset=0" in runs[-1]["detail"]
    assert "offset=250" in runs[0]["detail"]


def test_a_completed_sweep_is_logged(tmp_path, caplog):
    fetch = PagedFetch([250, 40])
    w = _worker(tmp_path, fetch)
    with caplog.at_level(logging.INFO, logger="app.harvester.worker"):
        w._harvest_phone_pair(*PAIR)
        assert "sweep complete" not in caplog.text   # offset 0: nothing swept
        caplog.clear()
        w._harvest_phone_pair(*PAIR)
    assert "sweep complete at offset 250" in caplog.text
    assert "cursor wrapped to 0" in caplog.text


# ---------------------------------------------------------------------------
# the query: paging is only sound over a stable order
# ---------------------------------------------------------------------------

def test_every_page_is_ordered_by_a_stable_key():
    """Without an explicit order, offset windows can skip or repeat rows."""
    params = _page_params("x='y'", 250, 0)
    assert params["$order"] == ":id"
    assert params["$limit"] == "250"


def test_the_offset_param_is_sent_only_when_walking():
    assert "$offset" not in _page_params("x='y'", 250, 0)
    assert _page_params("x='y'", 250, 250)["$offset"] == "250"


def test_fetch_license_records_threads_the_offset_into_the_query(
        monkeypatch):
    seen: dict = {}

    def fake_fetch_page(url, params, pinned_ip=""):
        seen["url"] = url
        seen["params"] = params
        return SourceStatus.SUCCESS, [], ""

    monkeypatch.setattr("app.phones.soda._fetch_page", fake_fetch_page)
    status, records, meta = fetch_license_records(
        SOURCE, WA_SLUG, "", 250, offset=500)
    assert status is SourceStatus.SUCCESS
    assert seen["url"] == WA_DATASET
    assert seen["params"]["$offset"] == "500"
    assert seen["params"]["$order"] == ":id"
    assert meta["offset"] == 500
    assert records == []


def test_a_negative_offset_reads_from_the_top(monkeypatch):
    seen: dict = {}

    def fake_fetch_page(url, params, pinned_ip=""):
        seen.update(params)
        return SourceStatus.SUCCESS, [], ""

    monkeypatch.setattr("app.phones.soda._fetch_page", fake_fetch_page)
    fetch_license_records(SOURCE, WA_SLUG, "", 250, offset=-10)
    assert "$offset" not in seen


@pytest.mark.parametrize("offset", [0, 250, 500])
def test_the_page_shape_is_the_same_for_every_window(offset):
    params = _page_params("x='y'", 250, offset)
    assert params["$limit"] == "250" and params["$order"] == ":id"
    assert params.get("$offset", "0") == str(offset)
