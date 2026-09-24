"""Calling evidence survives sheet changes and retired pool rows."""

import sqlite3
from datetime import datetime, timedelta, timezone

from app.phones.store import PhoneLeadsStore


def _stock(store, phone, state="WA"):
    store.add([{"phone": phone, "person_name": "Jane Smith",
                "business_name": "Acme", "trade_category": "GENERAL",
                "state": state, "source": "official"}])


def test_dial_and_outcome_are_owner_only_and_queryable_by_day(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    _stock(store, "5031110001")
    lead = store.serve("", "WA", "", 1, "alice")[0]
    assert store.record_call_event(lead["id"], "bob", "dialed") is None
    assert store.record_call_event(lead["id"], "alice", "dialed") is not None
    assert store.record_call_event(lead["id"], "alice", "not_interested") is not None
    day = store.call_activity("alice")
    assert day["dialed"] == 1
    assert day["outcomes"]["not_interested"] == 1
    assert day["events"][0]["phone"] == "+15031110001"
    assert day["date"] in store.call_days("alice")
    assert store.call_activity("bob")["events"] == []


def test_wrong_number_leaves_user_sheet_stays_archived_and_admin_recovers(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    _stock(store, "5031110001")
    lead = store.serve("", "WA", "", 1, "alice")[0]
    assert store.mark_wrong_number(lead["id"], "bob") is None
    assert store.mark_wrong_number(lead["id"], "alice") is not None
    assert store.list_owned("alice") == []
    assert store.pool_stats()["total"] == 0
    archived = store.list_wrong_archive()
    assert len(archived) == 1
    assert archived[0]["phone"] == "+15031110001"
    assert store.call_activity("alice")["outcomes"]["wrong_number"] == 1
    assert store.recover_wrong_number(archived[0]["id"]) is True
    assert store.list_wrong_archive() == []
    assert store.pool_stats()["total"] == 1


def test_wrong_number_suppresses_every_pool_row_with_same_phone(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add([
        {"phone": "5031110001", "business_name": "First", "trade_category": "GENERAL", "state": "WA"},
        {"phone": "5031110001", "business_name": "Second", "trade_category": "GENERAL", "state": "WA"},
    ])
    first = store.serve("", "WA", "", 1, "alice")[0]
    store.mark_wrong_number(first["id"], "alice")
    assert store.pool_stats()["total"] == 0
    assert store.serve("", "WA", "", 1, "bob") == []


def test_existing_lead_and_voicemail_buttons_feed_daily_outcomes(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    _stock(store, "5031110001")
    _stock(store, "5031110002")
    first, second = store.serve("", "WA", "", 2, "alice")
    store.mark_lead(first["id"], "alice")
    store.mark_voicemail(second["id"], "alice")
    activity = store.call_activity("alice")
    assert activity["outcomes"] == {"lead": 1, "voicemail": 1}


def test_sheet_accumulates_today_across_states_and_resets_next_day(tmp_path):
    path = str(tmp_path / "phones.db")
    store = PhoneLeadsStore(db_path=path)
    _stock(store, "5031110001", "WA")
    _stock(store, "5121110002", "TX")
    store.serve("", "WA", "", 1, "alice")
    store.serve("", "TX", "", 1, "alice")
    assert {lead["state"] for lead in store.list_owned("alice")} == {"WA", "TX"}
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE phone_lead_owners SET created_at = ? WHERE user_id = ?",
                     (yesterday + "T12:00:00", "alice"))
    assert store.list_owned("alice") == []


def test_past_day_history_is_queryable_after_new_day(tmp_path):
    path = str(tmp_path / "phones.db")
    store = PhoneLeadsStore(db_path=path)
    _stock(store, "5031110001")
    lead = store.serve("", "WA", "", 1, "alice")[0]
    store.record_call_event(lead["id"], "alice", "dialed")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE phone_call_events SET created_at = ? WHERE user_id = ?",
                     (yesterday + "T14:00:00", "alice"))
    assert store.call_activity("alice")["dialed"] == 0
    assert store.call_activity("alice", yesterday)["dialed"] == 1
    assert store.call_days("alice") == [yesterday]
