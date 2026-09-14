"""P7.5 — the phones CALLING workflow contracts.

A phone user works a trade-less sheet (state + quantity): call the number,
then ✓Lead / ☎Voicemail / 💾Store / 📝Note. These pin the store's action
methods (tiered voicemail parking, retire+suppress, saved snapshots), the
trade-less serve + coverage, and the API endpoints wrapping them all.
"""

from __future__ import annotations

from app.phones.service import phone_search
from app.phones.store import (
    MAX_VOICEMAILS,
    VOICEMAIL_COOLDOWN_DAYS,
    PhoneLeadsStore,
)


def _rec(phone: str, business: str = "Acme", trade: str = "GENERAL",
         city: str = "SEATTLE", state: str = "WA") -> dict:
    return {
        "phone": phone, "person_name": "SMITH, JANE",
        "business_name": business, "trade_category": trade,
        "city": city, "state": state, "source": "wa_license",
        "license_status": "ACTIVE", "source_url": "https://data.wa.gov/x",
    }


def _store(tmp_path) -> PhoneLeadsStore:
    return PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))


# ---------------------------------------------------------------------------
# trade-less serve — mixed trades, each row labelled
# ---------------------------------------------------------------------------

def test_trade_less_serve_mixed_trades(tmp_path):
    """The P7.5 form is state + quantity: slug='' serves the pool's mixed
    trades and every row carries its own trade."""
    store = _store(tmp_path)
    store.add([
        _rec("5031110001", "GC Co", "GENERAL"),
        _rec("5031110002", "Paint Co", "PAINTING/WALLCOVERING"),
        _rec("5121110003", "AC Co", "A/C Contractor", state="TX"),
    ])
    out = phone_search(store, trade="", state="WA", city="",
                       target=10, user_id="alice")
    assert [l["business_name"] for l in out["leads"]] == ["GC Co", "Paint Co"]
    assert {l["trade"] for l in out["leads"]} == {"gc", "painting"}


def test_trade_less_coverage_is_anything_stocking_the_state(tmp_path):
    """A trade-less search's coverage = every source covering the state —
    not the (empty) slug='' map."""
    store = _store(tmp_path)
    out = phone_search(store, trade="", state="TX", city="",
                       target=5, user_id="alice")
    assert out["coverage"] == ["tdlr_license"]
    out_wa = phone_search(store, trade="", state="WA", city="",
                          target=5, user_id="alice")
    assert out_wa["coverage"] == ["wa_license"]
    out_ca = phone_search(store, trade="", state="CA", city="",
                          target=5, user_id="alice")
    assert out_ca["coverage"] == ["cslb_portal"]
    # No state at all: everything, honestly named.
    out_any = phone_search(store, trade="", state="", city="",
                           target=5, user_id="alice")
    assert set(out_any["coverage"]) == {
        "wa_license", "tdlr_license", "cslb_portal",
    }


def test_trade_less_partial_reason_is_state_level(tmp_path):
    store = _store(tmp_path)
    store.add([_rec("5031110001")])
    out = phone_search(store, trade="", state="WA", city="",
                       target=3, user_id="alice")
    assert len(out["leads"]) == 1
    assert "harvester" in out["reason"]
    assert "state" in out["reason"]


# ---------------------------------------------------------------------------
# ✓Lead — snapshot to My Leads, retire + suppress for good
# ---------------------------------------------------------------------------

def test_mark_lead_snapshots_and_retires(tmp_path):
    """✓Lead: the row saves to the caller's account, the pool row is GONE,
    and the number is suppressed — no other user, no re-harvest, ever."""
    store = _store(tmp_path)
    store.add([_rec("5031110001", "Won Co")])
    lead = store.serve("gc", "", "", 10, "alice")[0]

    out = store.mark_lead(lead["id"], "alice")
    assert out == {"saved_id": out["saved_id"], "retired": True}

    # My Leads holds the full snapshot...
    saved = store.list_saved("alice", kind="lead")
    assert len(saved) == 1
    assert saved[0]["phone"] == "+15031110001"
    assert saved[0]["business_name"] == "Won Co"
    assert saved[0]["kind"] == "lead"
    # ...the pool row is deleted (not just unclaimed)...
    assert store.pool_stats()["total"] == 0
    assert store.list_owned("alice") == []
    # ...and a re-harvest of the same number is refused at add().
    counts = store.add([_rec("5031110001", "Won Co")])
    assert counts["inserted"] == 0
    assert counts["suppressed"] == 1


def test_mark_lead_owner_only(tmp_path):
    """Only the claimant can mark a lead; another user's row is a 404-style
    None, and the action never happens."""
    store = _store(tmp_path)
    store.add([_rec("5031110001")])
    lead = store.serve("gc", "", "", 10, "alice")[0]
    assert store.mark_lead(lead["id"], "bob") is None
    assert store.mark_lead(99999, "alice") is None
    # Nothing changed: the row is still in the pool, still alice's.
    assert store.list_owned("alice") and store.pool_stats()["total"] == 1


# ---------------------------------------------------------------------------
# ☎Voicemail — tiered parking, release, 4th retires
# ---------------------------------------------------------------------------

def test_voicemail_tiers_and_release(tmp_path):
    """Each voicemail parks the number for the tier's cooldown and RELEASES
    the claim — the row leaves the caller's sheet immediately."""
    store = _store(tmp_path)
    store.add([_rec("5031110001")])
    lead = store.serve("gc", "", "", 10, "alice")[0]

    out1 = store.mark_voicemail(lead["id"], "alice")
    assert out1 == {"retired": False, "voicemail_count": 1,
                    "cooldown_days": VOICEMAIL_COOLDOWN_DAYS[1]}
    # Released: off alice's sheet, and NOT restorable to anyone (resting).
    assert store.list_owned("alice") == []
    assert store.serve("gc", "", "", 10, "bob") == []
    assert store.unclaimed_count("gc") == 0  # resting, not servable


def test_voicemail_resting_expires_and_recirculates(tmp_path):
    """After the cooldown the resting row serves again — data reuse: the
    next caller (this user or another) gets the same number."""
    import sqlite3

    store = _store(tmp_path)
    store.add([_rec("5031110001")])
    lead = store.serve("gc", "", "", 10, "alice")[0]
    store.mark_voicemail(lead["id"], "alice")

    # Age the parking window out (the DB holds ISO timestamps; a direct
    # backdate is the honest way to cross the 14-day tier in a test).
    db = str(tmp_path / "phones.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE phone_leads SET voicemail_at = datetime('now', '-15 days')"
    )
    conn.commit()
    conn.close()

    # Bob — a DIFFERENT user — now serves the recycled number.
    bob = store.serve("gc", "", "", 10, "bob")
    assert [l["phone"] for l in bob] == ["+15031110001"]


def test_fourth_voicemail_retires_for_good(tmp_path):
    """The approved ladder: voicemail #4 = the number is deleted from the
    DB and retired to the suppression cache — the harvester can never
    re-add it (the deleted_emails pattern)."""
    store = _store(tmp_path)
    store.add([_rec("5031110001")])
    lead = store.serve("gc", "", "", 10, "alice")[0]

    for _n in range(1, MAX_VOICEMAILS):
        out = store.mark_voicemail(lead["id"], "alice")
        assert out["retired"] is False
        # Re-claim after each cooldown so the next voicemail can land.
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "phones.db"))
        conn.execute(
            "UPDATE phone_leads SET voicemail_at = "
            "datetime('now', '-365 days')"
        )
        conn.commit()
        conn.close()
        store.serve("gc", "", "", 10, "alice")

    out4 = store.mark_voicemail(lead["id"], "alice")
    assert out4 == {"retired": True, "voicemail_count": MAX_VOICEMAILS,
                    "cooldown_days": 0}
    # Gone from the pool AND banned from re-entry.
    assert store.pool_stats()["total"] == 0
    assert store.add([_rec("5031110001")])["suppressed"] == 1


# ---------------------------------------------------------------------------
# 💾Store + 📝Note — saved contacts survive pool retirement
# ---------------------------------------------------------------------------

def test_store_contact_stays_claimed(tmp_path):
    """💾Store saves the snapshot but the row STAYS the caller's — still on
    their sheet, never served to anyone else."""
    store = _store(tmp_path)
    store.add([_rec("5031110001")])
    lead = store.serve("gc", "", "", 10, "alice")[0]

    out = store.store_contact(lead["id"], "alice")
    assert out["retired"] is False
    assert len(store.list_saved("alice", kind="contact")) == 1
    # Still claimed: still on the sheet, invisible to bob.
    assert len(store.list_owned("alice")) == 1
    assert store.serve("gc", "", "", 10, "bob") == []


def test_saved_snapshot_survives_pool_retirement(tmp_path):
    """The saved copy is a real snapshot: retire the pool row (e.g. the
    number later turns out to be a lead for another user path) and the
    user's stored contact/lead keeps every field."""
    store = _store(tmp_path)
    store.add([_rec("5031110001", "Kept Co")])
    lead = store.serve("gc", "", "", 10, "alice")[0]
    store.store_contact(lead["id"], "alice")
    # Later the SAME number is retired out from under the pool entirely.
    store._retire(lead["id"], "+15031110001", "test_retire")

    saved = store.list_saved("alice")
    assert len(saved) == 1
    assert saved[0]["business_name"] == "Kept Co"
    assert saved[0]["phone"] == "+15031110001"


def test_note_lead_auto_stores_contact(tmp_path):
    """📝Note on a sheet lead auto-stores a contact and saves the note."""
    store = _store(tmp_path)
    store.add([_rec("5031110001")])
    lead = store.serve("gc", "", "", 10, "alice")[0]

    out = store.note_lead(lead["id"], "alice", "call back Tuesday 3pm")
    assert out["retired"] is False
    saved = store.list_saved("alice", kind="contact")
    assert len(saved) == 1
    assert saved[0]["note"] == "call back Tuesday 3pm"


def test_saved_note_edit_and_delete(tmp_path):
    store = _store(tmp_path)
    store.add([_rec("5031110001")])
    lead = store.serve("gc", "", "", 10, "alice")[0]
    store.mark_lead(lead["id"], "alice")

    saved = store.list_saved("alice", kind="lead")[0]
    assert store.set_saved_note(saved["id"], "alice", "updated note")
    assert store.list_saved("alice")[0]["note"] == "updated note"
    # Another user's note-edit/delete is refused.
    assert store.set_saved_note(saved["id"], "bob", "hijack") is False
    assert store.delete_saved(saved["id"], "bob") is False
    # The owner deletes their own row; the list empties.
    assert store.delete_saved(saved["id"], "alice") is True
    assert store.list_saved("alice") == []
