"""P3 — PhoneLeadsStore contracts.

The phones vertical's own phone_leads.db (campaigns.db pattern): phone
normalization, trade fold at ingest (P1), the P2 strict trade gate at serve,
and the dossier_owners-style exclusivity — a lead claimed by one user never
serves to another.
"""

from __future__ import annotations

from app.phones.store import (
    PhoneLeadsStore,
    normalize_phone,
    pretty_person_name,
)


# ---------------------------------------------------------------------------
# normalization helpers
# ---------------------------------------------------------------------------

def test_normalize_phone_us_forms():
    assert normalize_phone("5039573452") == "+15039573452"
    assert normalize_phone("1-512-563-7173") == "+15125637173"
    assert normalize_phone("(503) 957-3452") == "+15039573452"


def test_normalize_phone_drops_unusable():
    """A phone that isn't a US 10/11-digit number is an honest drop — never
    a mangled lead."""
    assert normalize_phone("") == ""
    assert normalize_phone("12345") == ""
    assert normalize_phone("call us") == ""


def test_pretty_person_name_flips_last_first():
    assert pretty_person_name("GUERRERO MARTINEZ, CARLOS I.") == \
        "Carlos I. Guerrero Martinez"
    assert pretty_person_name("JANE SMITH") == "JANE SMITH"
    assert pretty_person_name("") == ""


# ---------------------------------------------------------------------------
# add + serve
# ---------------------------------------------------------------------------

def _rec(phone: str, business: str = "Acme", trade: str = "GENERAL",
         city: str = "VANCOUVER", state: str = "WA") -> dict:
    return {
        "phone": phone, "person_name": "SMITH, JANE",
        "business_name": business, "trade_category": trade,
        "city": city, "state": state, "source": "wa_license",
        "license_status": "ACTIVE", "source_url": "https://data.wa.gov/x",
    }


def test_add_folds_trade_and_dedups(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    counts = store.add([_rec("5039573452"), _rec("5039573452")])
    assert counts == {"inserted": 1, "duplicate": 1, "dropped_bad_phone": 0}
    leads = store.serve("gc", "", "", 10, "u1")
    assert len(leads) == 1
    assert leads[0]["trade"] == "gc"          # P1 fold at ingest
    assert leads[0]["person_name"] == "Jane Smith"  # LAST, FIRST -> First Last
    assert leads[0]["phone"] == "+15039573452"


def test_add_drops_bad_phones_honestly(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    counts = store.add([_rec("not-a-phone"), _rec("5039573452")])
    assert counts["dropped_bad_phone"] == 1
    assert counts["inserted"] == 1


def test_serve_trade_gate_strict(tmp_path):
    """P2 rule on phones too: a trade-filtered serve only serves that trade;
    other-trade rows stay banked as pool inventory."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([
        _rec("5031110001", "GC Co", "GENERAL"),
        _rec("5031110002", "Paint Co", "PAINTING/WALLCOVERING"),
        _rec("5031110003", "Vague Co", ""),
    ])
    served = store.serve("gc", "", "", 10, "u1")
    assert [l["business_name"] for l in served] == ["GC Co"]
    # The painting row is inventory for a painting search:
    assert [l["business_name"] for l in store.serve("painting", "", "", 10, "u2")] \
        == ["Paint Co"]


def test_serve_location_filters_compose(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([
        _rec("5031110001", "A", city="VANCOUVER", state="WA"),
        _rec("5031110002", "B", city="SEATTLE", state="WA"),
        _rec("5121110003", "C", city="AUSTIN", state="TX"),
    ])
    assert [l["business_name"] for l in store.serve("", "WA", "", 10, "u1")] \
        == ["A", "B"]
    assert [l["business_name"] for l in store.serve("", "WA", "seattle", 10, "u1")] \
        == ["B"]


def test_serve_is_exclusive_across_users(tmp_path):
    """The dossier_owners rule: once a lead serves to a user, it is that
    user's inventory — a second user's search never re-serves it."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001"), _rec("5031110002")])
    alice = store.serve("gc", "", "", 1, "alice")
    assert len(alice) == 1
    # Bob gets the OTHER lead — never alice's.
    bob = store.serve("gc", "", "", 10, "bob")
    assert [l["id"] for l in bob] != [alice[0]["id"]]
    assert len(bob) == 1
    # Alice's own re-search still serves her lead back (idempotent claim) —
    # and now that bob owns the other, it is the ONLY thing she can get.
    assert [l["id"] for l in store.serve("gc", "", "", 10, "alice")] == \
        [alice[0]["id"]]
    # Nobody left after both are claimed.
    assert store.serve("gc", "", "", 10, "carol") == []
    assert store.unclaimed_count("gc") == 0


def test_serve_exclude_ids_prevents_run_duplicates(tmp_path):
    """The gap-fill re-serve only brings NEW rows — a run never serves the
    same lead twice (the pool serve's rows are excluded)."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001"), _rec("5031110002")])
    first = store.serve("gc", "", "", 1, "alice")
    second = store.serve("gc", "", "", 5, "alice",
                         exclude_ids=[l["id"] for l in first])
    assert [l["id"] for l in second] != [first[0]["id"]]
    assert len(first) + len(second) == 2


def test_list_owned_and_pool_stats(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001"), _rec("5031110002", trade="PAINTING")])
    store.serve("gc", "", "", 10, "alice")
    mine = store.list_owned("alice")
    assert len(mine) == 1 and mine[0]["trade"] == "gc"
    assert store.list_owned("bob") == []
    stats = store.pool_stats()
    assert stats["total"] == 2
    assert stats["claimed"] == 1
    assert stats["unclaimed"] == 1
    assert stats["by_trade"]["gc"] == 1
