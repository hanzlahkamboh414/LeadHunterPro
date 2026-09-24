"""The phone allowance counts durable claims across states and actions."""

import sqlite3
from datetime import datetime, timedelta, timezone

from app.phones.store import PhoneLeadsStore


def _records(n, state="WA", start=0):
    return [{
        "phone": f"503{(start + i):07d}", "business_name": f"Firm {start + i}",
        "trade_category": "GENERAL", "state": state,
        "source": "wa_license", "license_status": "ACTIVE",
    } for i in range(n)]


def test_default_allowance_accumulates_across_states_and_cannot_refund(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    store.add(_records(300) + _records(400, "OR", 300))
    assert store.daily_limit("alice") == 600
    assert len(store.serve("", "WA", "", 200, "alice")) == 200
    assert len(store.serve("", "OR", "", 400, "alice")) == 400
    assert store.daily_usage("alice") == 600
    assert store.daily_remaining("alice") == 0
    first = store.list_owned("alice", state="OR")[0]
    store.mark_voicemail(first["id"], "alice")
    assert store.daily_usage("alice") == 600
    assert store.serve("", "WA", "", 1, "alice") == []


def test_admin_override_and_new_day(tmp_path):
    path = str(tmp_path / "phones.db")
    store = PhoneLeadsStore(db_path=path)
    store.add(_records(5))
    store.set_daily_limit("alice", 2)
    assert store.daily_limit("alice") == 2
    assert len(store.serve("", "WA", "", 5, "alice")) == 2
    assert store.daily_remaining("alice") == 0
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE phone_claim_events SET created_at = ? WHERE user_id = ?",
                     (yesterday + "T12:00:00", "alice"))
    assert store.daily_remaining("alice") == 2
    assert len(store.serve("", "WA", "", 2, "alice")) == 2


def test_reopen_preserves_usage_and_limit(tmp_path):
    path = str(tmp_path / "phones.db")
    store = PhoneLeadsStore(db_path=path)
    store.add(_records(3))
    store.set_daily_limit("alice", 1)
    store.serve("", "WA", "", 1, "alice")
    reopened = PhoneLeadsStore(db_path=path)
    assert reopened.daily_limit("alice") == 1
    assert reopened.daily_remaining("alice") == 0
