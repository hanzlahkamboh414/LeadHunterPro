"""P10 — the harvester works the source-yield learning loop (hermetic).

The phones lane now: counts every SUCCESSFUL fetch as a (source, pair)
trial, credits a working stock only when NEW rows land, never picks or
re-verifies a yield-asleep pair, and lets the drop re-arm after the
staleness window so license boards' NEW licenses are never starved out.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.discovery.sources.status import SourceStatus
from app.harvester.store import SOURCE_MIN_TRIALS, source_segment
from app.phones.soda import fetchable_trade_coverage
from tests.harvester.test_scout_wiring import _NOW, _worker

SEG = source_segment("gc", "WA")


def _rec(phone: str = "5031110001") -> dict:
    return {
        "phone": phone, "person_name": "SMITH, JANE",
        "business_name": "Acme GC", "trade_category": "GENERAL",
        "city": "SEATTLE", "state": "WA", "source": "wa_license",
        "license_status": "ACTIVE", "source_url": "https://data.wa.gov/x",
    }


class FetchOK:
    """Records calls; serves the records it was given."""

    def __init__(self, records: list[dict]):
        self.records = records
        self.calls: list[tuple] = []

    def __call__(self, source_id, slug, city, limit):
        self.calls.append((source_id, slug, city, limit))
        return SourceStatus.SUCCESS, list(self.records), {}


class FetchFail:
    def __init__(self):
        self.calls = 0

    def __call__(self, source_id, slug, city, limit):
        self.calls += 1
        return SourceStatus.UNAVAILABLE, [], {"error": "source down"}


def _drop_pair(worker) -> None:
    """Prove the (source, pair) zero-yield: SOURCE_MIN_TRIALS trials, no
    working credit, drop fresh."""
    for _ in range(SOURCE_MIN_TRIALS):
        worker._store.record_source_dispatch("wa_license", SEG)


def _cool_down_every_other_pair(worker) -> None:
    coverage = fetchable_trade_coverage()
    for slug in coverage:
        for state in coverage[slug]:
            if (slug, state) != ("gc", "WA"):
                worker._store.record_run("phones", slug, state, "success", 0)


def _backdate_yield(worker, tmp_path, days: int) -> None:
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
# recording — dispatch at issue time, working only on NEW rows
# ---------------------------------------------------------------------------

def test_harvest_records_dispatch_and_working(tmp_path):
    worker = _worker(tmp_path, FetchOK([_rec()]))
    out = worker._harvest_phone_pair("gc", "WA")
    assert out["stocked"] == 1
    row = worker._store.source_yield_get("wa_license", SEG)
    assert row["trials"] == 1 and row["working"] == 1


def test_dup_only_fetch_is_a_trial_without_working(tmp_path):
    """The reward is pool value ADDED — a saturated re-fetch (all
    duplicates) earns a trial and nothing else."""
    fetch = FetchOK([_rec()])
    worker = _worker(tmp_path, fetch)
    worker._harvest_phone_pair("gc", "WA")
    worker._harvest_phone_pair("gc", "WA")
    row = worker._store.source_yield_get("wa_license", SEG)
    assert row["trials"] == 2 and row["working"] == 1


def test_source_error_is_not_a_yield_trial(tmp_path):
    """Hard failures are the scout circuit breaker's domain — never a yield
    verdict (a good source in a transient outage must not go asleep)."""
    worker = _worker(tmp_path, FetchFail())
    out = worker._harvest_phone_pair("gc", "WA")
    assert out["skipped"] == "source_error"
    assert worker._store.source_yield_get("wa_license", SEG) is None


def test_success_run_records_source(tmp_path):
    worker = _worker(tmp_path, FetchOK([_rec()]))
    worker._harvest_phone_pair("gc", "WA")
    assert worker._store.recent_runs("phones")[0]["source"] == "wa_license"


# ---------------------------------------------------------------------------
# gating — pair selection + re-verify respect the drop
# ---------------------------------------------------------------------------

def test_pick_pair_skips_yield_asleep_pair(tmp_path):
    worker = _worker(tmp_path, FetchOK([_rec()]))
    _drop_pair(worker)
    _cool_down_every_other_pair(worker)
    assert worker._pick_phone_pair() is None


def test_pick_pair_re_arms_after_staleness_window(tmp_path):
    worker = _worker(tmp_path, FetchOK([_rec()]))
    _drop_pair(worker)
    _cool_down_every_other_pair(worker)
    assert worker._pick_phone_pair() is None
    _backdate_yield(worker, tmp_path, days=31)
    assert worker._pick_phone_pair() == ("gc", "WA")


def test_midcycle_yield_skip_never_fetches(tmp_path):
    """A pair dropped between selection and harvest (the reverify lane's
    case) skips honestly — the fetch is never issued."""
    fetch = FetchOK([_rec()])
    worker = _worker(tmp_path, fetch)
    _drop_pair(worker)
    out = worker._harvest_phone_pair("gc", "WA")
    assert out["skipped"] == "source_yield_zero"
    assert fetch.calls == []
    assert worker._store.recent_runs("phones")[0][
        "outcome"] == "source_yield_zero"


def test_reverify_skips_yield_asleep_source(tmp_path):
    worker = _worker(tmp_path, FetchOK([_rec()]))
    _drop_pair(worker)
    worker._store.enqueue_reverify("gc", "WA", "stale")
    assert worker._process_reverify() == 0
    assert worker._store.recent_runs("phones")[0][
        "outcome"] == "source_yield_zero"


def test_stale_enqueue_skips_dropped_pair(tmp_path):
    """A stale pair whose source is yield-asleep is not even queued — a
    re-fetch of a proven-zero source is a guaranteed no-op."""
    worker = _worker(tmp_path, FetchOK([_rec()]))
    # The pair was harvested once (so it has run history)…
    worker._harvest_phone_pair("gc", "WA")
    # …long enough ago to be stale, with an empty pool (below the floor).
    old = (datetime.now(timezone.utc) - timedelta(days=40)) \
        .strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(str(tmp_path / "harvester.db"))
    conn.execute(
        "UPDATE harvest_runs SET created_at = ? WHERE vertical = 'phones'",
        (old,),
    )
    conn.commit()
    conn.close()
    assert worker._enqueue_stale_pairs() == 1  # baseline: it WOULD queue

    worker._store.take_reverify(10)
    # The baseline harvest credited a working stock on this row (and a
    # working row never drops) — clear it, then prove the zero-yield drop.
    worker._store.delete_source_yield("wa_license", SEG)
    _drop_pair(worker)
    # The drop re-arms at the pick, not in the queue — but a FRESH drop
    # means the queue must not refill.
    assert worker._enqueue_stale_pairs() == 0
