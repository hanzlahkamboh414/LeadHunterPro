"""P9 sub-inc 5 — the harvester works the scout-promoted pairs too.

``effective_trade_coverage`` replaces the hand-only ``TRADE_COVERAGE``
inside the worker: a PROMOTED scout source's (trade, state) pairs get
stocked like any hand pair, and a pair that loses its source mid-cycle
(the circuit breaker retiring it) is skipped honestly, never crashed on.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.harvester.store import HarvesterStore
from app.harvester.worker import HarvesterWorker
from app.phones import soda as soda_mod
from app.phones.soda import TRADE_COVERAGE
from app.phones.store import PhoneLeadsStore
from app.source_scout.store import ScoutStore
from tests.phones.test_scout_wiring import OR_PAYLOAD, _promoted_store

# Captured at import (same trick as test_worker.py): the worker's fake
# clock must track the real clock so record_run cooldowns line up.
_NOW = datetime.now(timezone.utc)


class ScoutFetch:
    """Records calls; serves one valid OR electrical record per call."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail

    def __call__(self, source_id, slug, city, limit, offset=0):
        self.calls.append((source_id, slug, city, limit, offset))
        if self.fail:
            from app.discovery.sources.status import SourceStatus
            return SourceStatus.UNAVAILABLE, [], {"error": "source down"}
        from app.discovery.sources.status import SourceStatus
        return SourceStatus.SUCCESS, [{
            "phone": "5039573450",
            "person_name": "SMITH, JANE",
            "business_name": "Acme",
            "trade_category": "Electrical Contractor",
            "city": "PORTLAND",
            "state": "OR",
            "source": source_id,
            "license_status": "ACTIVE",
            "source_url": "https://data.oregon.gov/resource/pzjw-h5rt.json",
        }], {}


def _worker(tmp_path, fetch):
    store = HarvesterStore(db_path=str(tmp_path / "harvester.db"))
    phone_store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    return HarvesterWorker(
        store, phone_store, lead_store=object(), pending_store=object(),
        fetch=fetch, research=lambda q: {"leads_found": 0,
                                         "working_leads": 0, "shortfall": 0},
        active_jobs=lambda: 0, now=lambda: _NOW, enabled=True,
        max_active=4, interval_s=0.01, phone_batch=250, email_batch=25,
        pair_cooldown_s=21600.0, min_pool_floor=100, staleness_days=30,
        daily_phone_quota=5000, daily_email_quota=2000,
    )


def _cool_down_hand_pairs(worker):
    """record_run every hand pair now, so the 6h cooldown removes them all
    from candidate selection and only the scout pair remains."""
    for slug in TRADE_COVERAGE:
        for state in TRADE_COVERAGE[slug]:
            worker._store.record_run("phones", slug, state, "success", 0)


def test_pick_pair_selects_the_scout_promoted_pair(tmp_path, monkeypatch):
    store = _promoted_store(tmp_path)  # electrical/OR
    monkeypatch.setattr(soda_mod, "_scout_store", lambda: store)
    worker = _worker(tmp_path, ScoutFetch())
    _cool_down_hand_pairs(worker)

    assert worker._pick_phone_pair() == ("electrical", "OR")


def test_harvest_stocks_the_scout_pair(tmp_path, monkeypatch):
    store = _promoted_store(tmp_path)
    monkeypatch.setattr(soda_mod, "_scout_store", lambda: store)
    fetch = ScoutFetch()
    worker = _worker(tmp_path, fetch)

    outcome = worker._harvest_phone_pair("electrical", "OR")

    assert outcome["skipped"] == ""
    assert outcome["fetched"] == 1
    assert outcome["stocked"] == 1
    # The fetch went to the scout source id (hand coverage has no OR pair).
    assert fetch.calls[0][0] == "or_ccb_license"
    assert fetch.calls[0][1] == "electrical"
    assert worker._phone_store.unclaimed_count("electrical", "OR") == 1


def test_harvest_skips_honestly_when_coverage_is_lost(tmp_path, monkeypatch):
    """The pair's scout source retired between selection and harvest (the
    circuit breaker): an honest skip + cooldown, never a crash."""
    store = ScoutStore(db_path=str(tmp_path / "source_scout.db"))
    store.propose("or_ccb_license", "soda", "OR CCB",
                  "https://data.oregon.gov/resource/pzjw-h5rt.json",
                  dict(OR_PAYLOAD), "playbook")
    # Never promoted — coverage for electrical/OR does not exist.
    monkeypatch.setattr(soda_mod, "_scout_store", lambda: store)
    fetch = ScoutFetch()
    worker = _worker(tmp_path, fetch)

    outcome = worker._harvest_phone_pair("electrical", "OR")

    assert outcome["skipped"] == "no_coverage"
    assert fetch.calls == []
    runs = worker._store.last_run_at("phones", "electrical", "OR")
    assert runs  # the skip is recorded, so the pair cools down too
