"""Phones search service contracts (P7: pure-SQL instant serve).

The search SERVES what the shared pool holds — instant, exclusive, nothing
else. Stocking is the harvester's job (P6): a short pool is an honest
partial with a "harvester is stocking this pair" reason, never a
synchronous license-board fetch inside the request.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# a stocked pool serves instantly, exclusively
# ---------------------------------------------------------------------------

def test_pool_serves_instantly(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_wa_record("5031110001", "A"), _wa_record("5031110002", "B")])
    out = phone_search(store, trade="GC", state="WA", city="",
                       target=2, user_id="alice")
    assert len(out["leads"]) == 2
    assert out["served_from_pool"] == 2
    assert out["fetched_live"] == 0 and out["stocked_new"] == 0
    assert out["reason"] == ""
    # Served rows are alice's EXCLUSIVE inventory — nobody else gets them.
    assert phone_search(store, trade="GC", state="WA", city="",
                        target=5, user_id="bob")["leads"] == []


def test_second_user_served_from_the_same_pool(tmp_path):
    """The vertical's core promise: two users searching the same trade each
    get their OWN rows from the shared pool — no re-fetch, no overlap."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_wa_record(f"503111000{i}", f"C{i}") for i in (1, 2)])
    out1 = phone_search(store, trade="GC", state="WA", city="",
                        target=1, user_id="alice")
    out2 = phone_search(store, trade="GC", state="WA", city="",
                        target=1, user_id="bob")
    assert len(out1["leads"]) == 1 and len(out2["leads"]) == 1
    assert out1["leads"][0]["phone"] != out2["leads"][0]["phone"]


def test_trade_gate_holds_at_serve(tmp_path):
    """A GC search never serves the painting rows the pool banks."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_wa_record("5031110001", "Paint Co",
                          trade_desc="PAINTING/WALLCOVERING")])
    out = phone_search(store, trade="GC", state="WA", city="",
                       target=5, user_id="alice")
    assert out["leads"] == []


# ---------------------------------------------------------------------------
# honest partial-serve reasons (stocking is the harvester's job now)
# ---------------------------------------------------------------------------

def test_short_pool_is_an_honest_partial(tmp_path):
    """Pool has 1 of 3: the search serves the 1, never fetches, and says
    honestly that the harvester is stocking the pair."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_wa_record("5031110001", "A")])
    out = phone_search(store, trade="GC", state="WA", city="",
                       target=3, user_id="alice")
    assert len(out["leads"]) == 1
    assert out["served_from_pool"] == 1
    assert out["fetched_live"] == 0
    assert "harvester" in out["reason"]
    assert "1/3" in out["reason"]


def test_empty_pool_with_coverage_is_honest(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    out = phone_search(store, trade="GC", state="WA", city="",
                       target=5, user_id="alice")
    assert out["leads"] == []
    assert out["coverage"] == ["wa_license"]
    assert "harvester" in out["reason"]


def test_uncovered_trade_is_honest_no_source(tmp_path):
    """A trade no license board covers (Lumber): the pool serves whatever it
    happens to hold, with an honest no-coverage reason."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    out = phone_search(store, trade="Lumber", state="TX", city="",
                       target=5, user_id="alice")
    assert out["leads"] == []
    assert out["coverage"] == []
    assert "no phone source covers" in out["reason"]


def test_state_without_coverage_is_honest(tmp_path):
    """Electrical in WA: WA's contractor dataset has no electrical rows —
    honest empty with the no-coverage reason."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    out = phone_search(store, trade="Electrical", state="WA", city="",
                       target=5, user_id="alice")
    assert out["coverage"] == []
    assert "no phone source covers" in out["reason"]


def test_state_only_raw_stock_is_not_promised_as_ready_leads(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    raw = _wa_record("6125550100", "North Star Roofing", trade_desc="")
    raw.update({"state": "MN", "source": "mn_dli_registration",
                "state_only": True})
    store.add([raw])
    out = phone_search(store, trade="", state="MN", city="",
                       target=10, user_id="alice")
    assert out["leads"] == []
    assert "pending trade verification" in out["reason"]
    assert "try again in a few minutes" not in out["reason"]
    assert store.pool_stats()["total"] == 1
