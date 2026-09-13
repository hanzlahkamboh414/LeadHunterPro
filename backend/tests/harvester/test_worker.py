"""P6 — HarvesterWorker contracts (hermetic: fake fetch/research lanes,
tmp SQLite stores, injectable clock). The phones lane stocks the pool
under quota + cooldown + demand ranking, the emails lane runs
harvest-time AI ONLY on logged demand, and an idle pass drains the
staleness re-verify queue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.harvester.store import HarvesterStore
from app.harvester.worker import HarvesterWorker
from app.phones.soda import (
    TDLR_TRADE_VALUES,
    TRADE_COVERAGE,
    WA_TRADE_VALUES,
    SourceStatus,
)
from app.phones.store import PhoneLeadsStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# Captured at import, NOT a fixed datetime: the worker's injectable clock
# must track the REAL clock so store rows written by record_run (which
# stamps real now) land inside the cooldown window for the fake clock too.
_NOW = datetime.now(timezone.utc)


def _trade_value(source_id: str, slug: str) -> str:
    """The source's real trade vocabulary for a slug — so a fetched record
    folds back to the SAME slug at ingest (normalize_trade round-trip)."""
    values = (WA_TRADE_VALUES if source_id == "wa_license"
              else TDLR_TRADE_VALUES)
    return values[slug][0]


class FakeFetch:
    """Records calls; returns ``n`` records per call, folding each to the
    requested slug (or a queued error via ``fail=True``)."""

    def __init__(self, n: int = 0, fail: bool = False):
        self.calls: list[tuple] = []
        self.n = n
        self.fail = fail

    def __call__(self, source_id, slug, city, limit):
        self.calls.append((source_id, slug, city, limit))
        if self.fail:
            return SourceStatus.UNAVAILABLE, [], {"error": "source down"}
        state = "WA" if source_id == "wa_license" else "TX"
        records = [
            {
                "phone": f"50395734{50 + i}",
                "person_name": "SMITH, JANE",
                "business_name": "Acme",
                "trade_category": _trade_value(source_id, slug),
                "city": "VANCOUVER" if state == "WA" else "AUSTIN",
                "state": state,
                "source": source_id,
                "license_status": "ACTIVE",
                "source_url": "https://example.gov/x",
            }
            for i in range(self.n)
        ]
        return SourceStatus.SUCCESS, records, {}


class FakeResearch:
    def __init__(self):
        self.calls: list = []

    def __call__(self, query):
        self.calls.append(query)
        return {"leads_found": 7, "working_leads": 6, "shortfall": 0}


def _worker(tmp_path, *, fetch=None, research=None, active=lambda: 0,
            enabled=True, **kw):
    store = HarvesterStore(db_path=str(tmp_path / "harvester.db"))
    phone_store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    defaults = dict(
        fetch=fetch or FakeFetch(),
        research=research or FakeResearch(),
        active_jobs=active,
        now=lambda: _NOW,
        enabled=enabled,
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
        store, phone_store, lead_store=object(), pending_store=object(),
        **defaults,
    )


# ---------------------------------------------------------------------------
# kill-switch + admission control
# ---------------------------------------------------------------------------

def test_disabled_worker_skips_everything(tmp_path):
    fetch = FakeFetch(n=1)
    research = FakeResearch()
    worker = _worker(tmp_path, fetch=fetch, research=research, enabled=False)
    stats = worker.run_once()
    assert stats["skipped"] == "disabled"
    assert fetch.calls == [] and research.calls == []


def test_busy_worker_defers_to_user_searches(tmp_path):
    """Admission control: with the pipeline budget full of live user jobs,
    the harvester steps aside — a pass does no work at all."""
    fetch = FakeFetch(n=1)
    research = FakeResearch()
    worker = _worker(tmp_path, fetch=fetch, research=research,
                     active=lambda: 4)
    stats = worker.run_once()
    assert stats["skipped"] == "busy"
    assert fetch.calls == [] and research.calls == []


# ---------------------------------------------------------------------------
# phones lane
# ---------------------------------------------------------------------------

def test_phones_lane_stocks_the_emptiest_pair(tmp_path):
    fetch = FakeFetch(n=1)
    worker = _worker(tmp_path, fetch=fetch)
    stats = worker.run_once()

    phones = stats["phones"]
    assert phones["skipped"] == ""
    assert phones["pair"] is not None
    assert phones["fetched"] == 1
    assert phones["stocked"] == 1
    # The fetch went to a genuinely covered pair, within the batch limit.
    source_id, slug, city, limit = fetch.calls[0]
    assert TRADE_COVERAGE[slug] and source_id in TRADE_COVERAGE[slug].values()
    assert limit == 250
    # The row is genuinely in that pair's pool, and the quota counter moved.
    p_slug, p_state = phones["pair"]
    assert worker._phone_store.unclaimed_count(p_slug, p_state) == 1
    assert worker._store.quota_used("phones") == 1


def test_phones_lane_prefers_demanded_pair(tmp_path):
    """Two equally empty covered pairs; the one users searched for wins."""
    worker = _worker(tmp_path)
    worker._store.record_demand("roofing", "WA")  # covered (WA licenses roofing)
    assert worker._pick_phone_pair() == ("roofing", "WA")


def test_phones_lane_state_demand_boosts_whole_state(tmp_path):
    """P7.5: a trade-less search records a STATE-level demand row (trade='')
    — its weight boosts EVERY covered pair of that state, so 'TX, 100
    numbers' raises the whole state's stocking, any trade."""
    worker = _worker(tmp_path)
    worker._store.record_demand("", "TX")
    slug, state = worker._pick_phone_pair()
    assert state == "TX"
    assert TRADE_COVERAGE[slug]["TX"] == "tdlr_license"


def test_phones_lane_pair_cooldown_blocks_reharvest(tmp_path):
    """A pair harvested just now is inside the cooldown — with EVERY pair
    cooled down there is no candidate, and the lane skips honestly."""
    worker = _worker(tmp_path)
    for slug in TRADE_COVERAGE:
        for state in TRADE_COVERAGE[slug]:
            worker._store.record_run("phones", slug, state, "success", 1)
    stats = worker.run_once()
    assert stats["phones"]["skipped"] == "no_candidate"
    assert stats["phones"]["stocked"] == 0


def test_phones_lane_daily_quota_is_hard(tmp_path):
    worker = _worker(tmp_path, fetch=FakeFetch(n=1))
    worker._store.record_stocked("phones", 5000)  # today's quota spent
    stats = worker.run_once()
    assert stats["phones"]["skipped"] == "quota_exhausted"
    assert stats["phones"]["stocked"] == 0


def test_phones_lane_source_error_backs_off(tmp_path):
    """An unavailable source is an honest skip + a recorded run — the pair
    cools down instead of being retried every pass."""
    worker = _worker(tmp_path, fetch=FakeFetch(fail=True))
    stats = worker.run_once()
    assert stats["phones"]["skipped"] == "source_error"
    assert stats["phones"]["stocked"] == 0
    runs = worker._store.recent_runs("phones")
    assert runs and runs[0]["outcome"] == "source_error"
    assert runs[0]["stocked"] == 0


# ---------------------------------------------------------------------------
# emails lane (demand-gated harvest-time AI)
# ---------------------------------------------------------------------------

def test_emails_lane_researches_demanded_pair(tmp_path):
    research = FakeResearch()
    worker = _worker(tmp_path, research=research)
    worker._store.record_demand("roofing", "TX")

    stats = worker.run_once()
    emails = stats["emails"]
    assert emails["pair"] == ("roofing", "TX")
    assert emails["stocked"] == 7
    assert len(research.calls) == 1
    q = research.calls[0]
    assert q.trade == "Roofing"          # human-readable trade label
    assert q.location == "Texas"         # full state name, search vocabulary
    assert q.target_emails == 25         # min(batch, quota room)
    assert worker._store.quota_used("emails") == 7


def test_emails_lane_never_spends_without_demand(tmp_path):
    """The AI lane is demand-gated: no logged search, no research spend."""
    research = FakeResearch()
    worker = _worker(tmp_path, research=research)
    stats = worker.run_once()
    assert stats["emails"]["skipped"] == "no_demand"
    assert research.calls == []


def test_emails_lane_daily_quota_is_hard(tmp_path):
    research = FakeResearch()
    worker = _worker(tmp_path, research=research)
    worker._store.record_demand("roofing", "TX")
    worker._store.record_stocked("emails", 2000)  # today's quota spent
    stats = worker.run_once()
    assert stats["emails"]["skipped"] == "quota_exhausted"
    assert research.calls == []


def test_emails_lane_research_error_is_honest(tmp_path):
    def boom(query):
        raise RuntimeError("ai down")
    worker = _worker(tmp_path, research=boom)
    worker._store.record_demand("roofing", "TX")
    stats = worker.run_once()
    assert stats["emails"]["skipped"] == "error"
    assert stats["emails"]["stocked"] == 0
    assert worker._store.recent_runs("emails")[0]["outcome"] == "error"


# ---------------------------------------------------------------------------
# staleness re-verify
# ---------------------------------------------------------------------------

def test_idle_pass_drains_reverify_queue(tmp_path):
    """Both lanes empty (no demand, every pair cooled down) -> the queued
    stale pair is re-harvested, cooldown waived, quota still respected."""
    fetch = FakeFetch(n=1)
    worker = _worker(tmp_path, fetch=fetch)
    for slug in TRADE_COVERAGE:
        for state in TRADE_COVERAGE[slug]:
            worker._store.record_run("phones", slug, state, "success", 1)
    worker._store.enqueue_reverify("roofing", "WA", "stale")

    stats = worker.run_once()
    assert stats["phones"]["skipped"] == "no_candidate"
    assert stats["emails"]["skipped"] == "no_demand"
    assert stats["reverified"] == 1
    # The re-verify harvest really stocked the stale pair.
    assert worker._phone_store.unclaimed_count("roofing", "WA") == 1
    assert worker._store.reverify_pending() == 0


def test_stale_pairs_get_enqueued(tmp_path):
    """A pair harvested >staleness_days ago whose pool fell below the floor
    is queued for re-verify; the pending queue row is idempotent."""
    worker = _worker(tmp_path)
    # Direct row with an old created_at: harvested 40 days ago, pool now empty.
    conn = worker._store._conn()
    conn.execute(
        """
        INSERT INTO harvest_runs
            (vertical, trade, state, outcome, stocked, detail, created_at)
        VALUES ('phones', 'roofing', 'WA', 'success', 100, '', ?)
        """,
        ((_NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S"),),
    )
    conn.commit()
    conn.close()

    assert worker._enqueue_stale_pairs() == 1
    assert worker._store.reverify_pending() == 1
    # Idempotent while pending.
    assert worker._enqueue_stale_pairs() == 1
    assert worker._store.reverify_pending() == 1
