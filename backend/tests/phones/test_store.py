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
    assert counts["inserted"] == 1
    assert counts["duplicate"] == 1
    assert counts["dropped_bad_phone"] == 0
    assert counts["suppressed"] == 0  # P7.5: banned-number counter (honest 0)
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


def test_explicit_state_only_record_never_infers_trade_from_name(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    record = _rec("6125550100", "North Star Construction", "", state="MN")
    record["state_only"] = True
    assert store.add([record])["inserted"] == 1
    assert store.unclaimed_count("gc", "MN") == 0
    assert store.unclaimed_count("", "MN") == 0
    assert store.serve("", "MN", "", 1, "user") == []
    assert store.servable_by_state() == {}
    assert store.pool_stats()["total"] == 1  # retained for verification


def test_business_and_verified_trade_required_for_new_claim(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    no_business = _rec("5031110001", "", "ROOFING")
    no_trade = _rec("5031110002", "North Star Roofing", "")
    no_trade["state_only"] = True
    store.add([no_business, no_trade, _rec("5031110003", "Ready Co", "ROOFING")])

    assert store.unclaimed_count("", "WA") == 1
    assert store.servable_by_state() == {"WA": 1}
    assert [r["business_name"] for r in store.serve("", "WA", "", 10, "u1")] == [
        "Ready Co"]
    assert store.pool_stats()["total"] == 3
    assert store.pending_trade_enrichment(10)[0]["business_name"] == \
        "North Star Roofing"


def test_trade_resolution_promotes_only_with_evidence(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    rec = _rec("6125550100", "North Star Roofing", "", state="MN")
    rec["state_only"] = True
    store.add([rec])
    lead = store.pending_trade_enrichment(1)[0]

    assert not store.set_trade_resolution(
        lead["id"], trade="roofing", evidence_url="", evidence_kind="website")
    assert store.serve("", "MN", "", 1, "u1") == []
    assert store.set_trade_resolution(
        lead["id"], trade="roofing",
        evidence_url="https://northstarroofing.com/services",
        evidence_kind="company_website")
    served = store.serve("roofing", "MN", "", 1, "u1")
    assert len(served) == 1
    assert served[0]["business_name"] == "North Star Roofing"
    assert served[0]["person_name"] == "Jane Smith"  # source value unchanged
    assert served[0]["trade_evidence_url"] == \
        "https://northstarroofing.com/services"


def test_missing_trade_is_not_inferred_from_business_name(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001", "North Star Roofing", "")])
    assert store.serve("roofing", "WA", "", 1, "u1") == []
    assert len(store.pending_trade_enrichment(1)) == 1


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
    """State and city filters narrow the serve. Each probe gets its own pool:
    with a one-shot serve the same rows cannot be probed twice (the first
    serve owns them, and owned rows serve to nobody)."""
    rows = [
        _rec("5031110001", "A", city="VANCOUVER", state="WA"),
        _rec("5031110002", "B", city="SEATTLE", state="WA"),
        _rec("5121110003", "C", city="AUSTIN", state="TX"),
    ]
    by_state = PhoneLeadsStore(db_path=str(tmp_path / "state.db"))
    by_state.add(rows)
    assert [l["business_name"] for l in by_state.serve("", "WA", "", 10, "u1")] \
        == ["A", "B"]

    by_city = PhoneLeadsStore(db_path=str(tmp_path / "city.db"))
    by_city.add(rows)
    assert [l["business_name"] for l in by_city.serve("", "WA", "seattle", 10, "u1")] \
        == ["B"]


def test_serve_is_exclusive_across_users(tmp_path):
    """One-shot serve: once a lead serves to a user it never serves again —
    not to a second user, and not back to the first one."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001"), _rec("5031110002")])
    alice = store.serve("gc", "", "", 1, "alice")
    assert len(alice) == 1
    # Bob gets the OTHER lead — never alice's.
    bob = store.serve("gc", "", "", 10, "bob")
    assert [l["id"] for l in bob] != [alice[0]["id"]]
    assert len(bob) == 1
    # The user's OWN re-search is empty too — the pool has nothing FRESH for
    # her, and her earlier lead is not served back (one-shot serve, the
    # user's policy: purana data kisi ko bhi na mile).
    assert store.serve("gc", "", "", 10, "alice") == []
    # Her claim is untouched by that: the lead is still hers to dial.
    assert [l["id"] for l in store.list_owned("alice")] == [alice[0]["id"]]
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


def test_list_owned_shows_all_todays_batches(tmp_path):
    """Today's call sheet retains claims from multiple searches."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001", "One"), _rec("5031110002", "Two"),
               _rec("5031110003", "Three")])
    first = store.serve("gc", "", "", 2, "alice")
    assert {l["business_name"] for l in store.list_owned("alice")} == \
        {"One", "Two"}
    second = store.serve("gc", "", "", 5, "alice")  # only the fresh one left
    assert [l["business_name"] for l in second] == ["Three"]
    assert {l["business_name"] for l in store.list_owned("alice")} == {"One", "Two", "Three"}


def test_twice_in_one_second_is_still_two_batches(tmp_path):
    """Two searches in the same wall-clock second must not collapse into one
    batch: the stamp's microsecond + random suffix makes every serve call its
    own batch. Without this, a fast repeat click would show the union."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001"), _rec("5031110002")])
    store.serve("gc", "", "", 1, "alice")
    store.serve("gc", "", "", 1, "alice")
    mine = store.list_owned("alice")
    assert len(mine) == 2


def test_replaced_batch_numbers_still_exclusive(tmp_path):
    """Hidden does not mean released: numbers a second search pushed off the
    sheet still serve to nobody — the ownership row lives on."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001"), _rec("5031110002"), _rec("5031110003")])
    store.serve("gc", "", "", 2, "alice")   # batch 1: ids 1, 2
    store.serve("gc", "", "", 5, "alice")   # batch 2: id 3
    # Fresh pool is now empty even for a never-before-seen user.
    assert store.serve("gc", "", "", 10, "carol") == []
    assert store.unclaimed_count("gc") == 0
    stats = store.pool_stats()
    assert stats["claimed"] == 3 and stats["unclaimed"] == 0


def test_claim_visibility_by_user_reports_hidden(tmp_path):
    """The admin report's honest split: what each user's sheet SHOWS (their
    latest batch) vs the stock hiding behind it (still owned, not shown).
    """
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001"), _rec("5031110002"), _rec("5031110003")])
    store.serve("gc", "", "", 2, "alice")
    store.serve("gc", "", "", 5, "alice")   # pushes the first 2 off-screen
    rows = {r["user_id"]: r for r in store.claim_visibility_by_user()}
    assert rows["alice"]["total"] == 3
    assert rows["alice"]["visible"] == 3
    assert rows["alice"]["hidden"] == 0
    assert "bob" not in rows


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


def test_servable_by_state_counts_only_fresh_rows(tmp_path):
    """The Location counts ("is location mein kitna naya data hai"): per
    state, the rows that would serve RIGHT NOW. A state whose numbers were
    already handed out reads 0 while the pool file still holds them — the
    two numbers are deliberately different, never conflated."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([
        _rec("5031110001", "WA one", state="WA"),
        _rec("5031110002", "WA two", state="WA"),
        _rec("5121110003", "TX one", state="TX"),
        _rec("5121110004", "TX two", state="TX"),
    ])
    assert store.servable_by_state() == {"WA": 2, "TX": 2}

    # Claim both WA rows: nothing fresh is left there, and TX is untouched.
    served = store.serve("", "WA", "", 10, "alice")
    assert len(served) == 2
    assert store.servable_by_state() == {"TX": 2}
    # The raw pool still holds them (by_state) — the honest difference.
    assert store.pool_stats()["by_state"]["WA"] == 2
    # A state the pool never stocked is absent from the map, not "unknown".
    assert "OR" not in store.servable_by_state()


def test_servable_by_state_excludes_parked_rows(tmp_path):
    """A voicemail-parked number is resting: it serves to nobody during the
    cooldown, so it must not be advertised as available either."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001", state="WA")])
    lead = store.serve("", "WA", "", 10, "alice")[0]
    store.mark_voicemail(lead["id"], "alice")
    assert store.servable_by_state() == {}


def test_pool_stats_counts_do_not_drift(tmp_path):
    """claimed + unclaimed must equal total EXACTLY, before and after a
    retirement: the numbers are counted over one row set, never derived by
    subtracting an ownership count that a retired lead can inflate (29 stale
    ownership rows were found live on 2026-09-16)."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001", state="WA"), _rec("5031110002", state="TX")])
    lead = store.serve("", "WA", "", 10, "alice")[0]
    stats = store.pool_stats()
    assert stats["total"] == 2 and stats["claimed"] == 1
    assert stats["claimed"] + stats["unclaimed"] == stats["total"]

    store.mark_lead(lead["id"], "alice")  # ✓Lead — the row leaves the pool
    stats = store.pool_stats()
    assert stats["total"] == 1 and stats["claimed"] == 0
    assert stats["claimed"] + stats["unclaimed"] == stats["total"] == 1
    assert stats["unclaimed"] == 1


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


def test_emailless_leads_covers_claimed_and_shared(tmp_path):
    """The Overture backfill's working set: EVERY email-less lead, claimed or
    not — unlike pending_enrichment's claimed-first queue."""
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([_rec("5031110001"), _rec("5031110002"), _rec("5031110003")])
    store.serve("gc", "", "", 1, "alice")  # claims exactly one

    rows = store.emailless_leads()
    assert len(rows) == 3  # claimed or shared, all three lack an email
    assert {r["phone"] for r in rows} == {"+15031110001", "+15031110002",
                                          "+15031110003"}  # E.164, as stored
    # The join keys the backfill needs are present.
    for r in rows:
        assert r["id"] and r["business_name"] and "trade" in r

    # A lead that gains an email leaves the working set.
    store.set_enrichment(
        rows[0]["id"], email="info@acme.com",
        email_source="overture", website="https://acme.com",
    )
    assert len(store.emailless_leads()) == 2
