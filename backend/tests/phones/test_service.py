"""P3 — phone_search service contracts (pool-first serve, live gap-fill).

The P7 shape in place from day one: serve what the pool holds (pure SQL),
fetch ONLY the gap live, stock EVERYTHING fetched, keep honest telemetry.
The SODA fetcher is faked — these tests pin the flow, not the network.
"""

from __future__ import annotations

from app.discovery.sources.status import SourceStatus
from app.phones import service
from app.phones.service import phone_search
from app.phones.store import PhoneLeadsStore


def _wa_record(phone: str, business: str, trade_desc: str = "GENERAL",
               city: str = "SEATTLE") -> dict:
    return {
        "phone": phone, "person_name": "SMITH, JANE",
        "business_name": business, "trade_category": trade_desc,
        "city": city, "state": "WA", "source": "wa_license",
        "license_status": "ACTIVE", "source_url": "https://data.wa.gov/x",
    }


def _fake_soda(monkeypatch, batches: dict[str, list[dict]]):
    """Fake fetch_license_records keyed by source_id (one batch per source)."""
    def _fake(source_id, slug, city="", limit=200):
        records = batches.get(source_id, [])
        return (SourceStatus.SUCCESS, records,
                {"source": source_id, "rows_fetched": len(records)})

    monkeypatch.setattr(service, "fetch_license_records", _fake)


# ---------------------------------------------------------------------------
# pool-first: a stocked pool serves instantly, no live fetch
# ---------------------------------------------------------------------------

def test_pool_serves_without_live_fetch(tmp_path, monkeypatch):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_wa_record("5031110001", "A"), _wa_record("5031110002", "B")])

    def _should_not_fetch(source_id, slug, city="", limit=200):
        raise AssertionError("pool has servable leads — no live fetch needed")

    monkeypatch.setattr(service, "fetch_license_records", _should_not_fetch)
    out = phone_search(store, trade="GC", state="WA", city="",
                       target=2, user_id="alice")
    assert len(out["leads"]) == 2
    assert out["served_from_pool"] == 2
    assert out["fetched_live"] == 0


# ---------------------------------------------------------------------------
# gap-fill: short pool triggers one live fetch, everything is stocked
# ---------------------------------------------------------------------------

def test_gap_fill_fetches_and_stocks(tmp_path, monkeypatch):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_wa_record("5031110001", "A")])
    fresh = [
        _wa_record("5031110002", "B"),
        _wa_record("5031110003", "C"),
        # Wrong trade from the source (rare, but honest banking when it
        # happens): stocked as painting inventory, never served to a GC run.
        _wa_record("5031110004", "Paint Co", trade_desc="PAINTING/WALLCOVERING"),
        # Unusable phone: honestly dropped, counted.
        _wa_record("call-us", "D"),
    ]
    _fake_soda(monkeypatch, {"wa_license": fresh})

    out = phone_search(store, trade="GC", state="WA", city="",
                       target=3, user_id="alice")
    assert len(out["leads"]) == 3            # 1 pool + 2 live
    assert out["served_from_pool"] == 1
    assert out["fetched_live"] == 4
    assert out["stocked_new"] == 3           # B, C, Paint Co
    assert out["dropped_bad_phone"] == 1
    assert out["reason"] == ""
    # The painting row is banked pool inventory (P2 rule)...
    assert store.pool_stats()["by_trade"]["painting"] == 1
    # ...and every row of this run is now alice's exclusive inventory.
    assert store.serve("gc", "WA", "", 10, "bob") == []


def test_second_search_served_from_newly_stocked_pool(tmp_path, monkeypatch):
    """The vertical's core promise: a second user searching the same trade
    gets served from the pool the FIRST search stocked — no re-fetch."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    _fake_soda(monkeypatch, {"wa_license": [_wa_record(f"50311100{i:02}", f"C{i}")
                                            for i in range(1, 4)]})
    out1 = phone_search(store, trade="GC", state="WA", city="",
                        target=2, user_id="alice")
    assert out1["fetched_live"] == 3

    def _should_not_fetch(source_id, slug, city="", limit=200):
        raise AssertionError("pool was stocked by the first run — no fetch")

    monkeypatch.setattr(service, "fetch_license_records", _should_not_fetch)
    out2 = phone_search(store, trade="GC", state="WA", city="",
                        target=1, user_id="bob")
    assert len(out2["leads"]) == 1
    assert out2["served_from_pool"] == 1


# ---------------------------------------------------------------------------
# honest coverage + failure reasons
# ---------------------------------------------------------------------------

def test_uncovered_trade_is_honest_no_source(tmp_path, monkeypatch):
    """A trade no license board covers (Lumber) never triggers a fetch; the
    pool serves whatever it happens to hold, with an honest reason."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))

    def _should_not_fetch(source_id, slug, city="", limit=200):
        raise AssertionError("no source covers this trade — never fetch")

    monkeypatch.setattr(service, "fetch_license_records", _should_not_fetch)
    out = phone_search(store, trade="Lumber", state="TX", city="",
                       target=5, user_id="alice")
    assert out["leads"] == []
    assert out["coverage"] == []
    assert "no phone source covers" in out["reason"]


def test_state_without_coverage_is_honest(tmp_path, monkeypatch):
    """Electrical in WA: WA's contractor dataset has no electrical rows (a
    separate WA program licenses them) — honest empty, no fetch."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))

    def _should_not_fetch(source_id, slug, city="", limit=200):
        raise AssertionError("no WA source for electrical — never fetch")

    monkeypatch.setattr(service, "fetch_license_records", _should_not_fetch)
    out = phone_search(store, trade="Electrical", state="WA", city="",
                       target=5, user_id="alice")
    assert out["coverage"] == []
    assert "no phone source covers" in out["reason"]


def test_dead_source_reports_shortfall_honestly(tmp_path, monkeypatch):
    """The source is down (UNAVAILABLE): the search returns the pool's rows
    and an honest shortfall reason — never an exception, never fake data."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))

    def _dead(source_id, slug, city="", limit=200):
        return SourceStatus.UNAVAILABLE, [], {"source": source_id, "error": "dns"}

    monkeypatch.setattr(service, "fetch_license_records", _dead)
    out = phone_search(store, trade="GC", state="WA", city="",
                       target=5, user_id="alice")
    assert out["leads"] == []
    assert out["fetched_live"] == 0
    assert "fewer usable leads" in out["reason"]
