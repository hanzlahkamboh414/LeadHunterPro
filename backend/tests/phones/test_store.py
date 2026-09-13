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


# ---------------------------------------------------------------------------
# enrichment (phone -> email)
# ---------------------------------------------------------------------------

def test_pre_enrichment_db_migrates_additively(tmp_path):
    """A phone_leads.db created BEFORE the email columns keeps every row and
    gains the four new columns (the users.category ALTER pattern)."""
    import sqlite3

    db = str(tmp_path / "phones.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE phone_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            person_name TEXT NOT NULL DEFAULT '',
            business_name TEXT NOT NULL DEFAULT '',
            trade TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            license_status TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (phone, business_name)
        )
    """)
    conn.execute(
        "INSERT INTO phone_leads (phone, business_name, trade, created_at,"
        " updated_at) VALUES ('5039573452', 'Legacy Co', 'gc', 'x', 'y')"
    )
    conn.commit()
    conn.close()

    store = PhoneLeadsStore(db_path=db)  # the ALTER runs here
    served = store.serve("gc", "", "", 10, "u1")
    assert len(served) == 1
    assert served[0]["business_name"] == "Legacy Co"
    assert served[0]["email"] == ""          # new columns, honest defaults
    assert served[0]["enriched_at"] == ""


def test_pending_enrichment_claimed_first_and_set_enrichment(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001"), _rec("5031110002"), _rec("5031110003")])
    store.serve("gc", "", "", 1, "alice")  # claims exactly one

    # Only the CLAIMED lead is work for the enricher.
    pending = store.pending_enrichment(10)
    assert [l["id"] for l in pending] == \
        [l["id"] for l in store.list_owned("alice")]
    assert len(pending) == 1

    # A found email is stamped with its provenance...
    store.set_enrichment(
        pending[0]["id"], email="info@acme.com",
        email_source="website", website="https://acme.com",
    )
    lead = store.list_owned("alice")[0]
    assert lead["email"] == "info@acme.com"
    assert lead["email_source"] == "website"
    assert lead["website"] == "https://acme.com"
    assert lead["enriched_at"] != ""

    # ...and an enriched lead (found OR miss) never returns to the queue.
    assert store.pending_enrichment(10) == []
    # claimed_only=False reaches the unclaimed inventory too.
    assert len(store.pending_enrichment(10, claimed_only=False)) == 2
