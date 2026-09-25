"""Tenant-scoped phone claims on a tagged unified database copy."""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token
from app.auth.activity import ActivityStore
from app.auth.models import UserStore
from app.api.v1.phones import router as phones_router
from app.phones.store import PhoneLeadsStore
from scripts.prepare_unified_database import build_unified_database


def _tag_claim_tables(path: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tenants (id TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO tenants (id) VALUES (?)",
            [("tenant-a",), ("tenant-b",)],
        )
        for table in (
            "phone_lead_owners", "phone_claim_events", "phone_call_events",
            "phone_daily_limits",
        ):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'tenant-a'")


def _record(number: str) -> dict:
    return {
        "phone": number,
        "business_name": f"Business {number}",
        "trade_category": "GENERAL",
        "state": "WA",
        "source": "wa_license",
        "license_status": "ACTIVE",
    }


def test_one_user_has_separate_tenant_call_sheets_and_usage(tmp_path):
    path = str(tmp_path / "phones.db")
    store = PhoneLeadsStore(db_path=path)
    _tag_claim_tables(path)
    store.add([_record("5035550101"), _record("5035550102")])

    first = store.serve("", "WA", "", 1, "same-user", tenant_id="tenant-a")
    second = store.serve("", "WA", "", 1, "same-user", tenant_id="tenant-b")

    assert len(first) == len(second) == 1
    assert first[0]["id"] != second[0]["id"]
    assert [row["id"] for row in store.list_owned("same-user", tenant_id="tenant-a")] == [first[0]["id"]]
    assert [row["id"] for row in store.list_owned("same-user", tenant_id="tenant-b")] == [second[0]["id"]]
    assert store.daily_usage("same-user", tenant_id="tenant-a") == 1
    assert store.daily_usage("same-user", tenant_id="tenant-b") == 1


def test_claim_is_globally_exclusive_across_tenants(tmp_path):
    path = str(tmp_path / "phones.db")
    store = PhoneLeadsStore(db_path=path)
    _tag_claim_tables(path)
    store.add([_record("5035550101")])

    assert len(store.serve("", "WA", "", 1, "alice", tenant_id="tenant-a")) == 1
    assert store.serve("", "WA", "", 1, "bob", tenant_id="tenant-b") == []


def test_same_phone_on_two_business_rows_is_only_claimed_once(tmp_path):
    path = str(tmp_path / "phones.db")
    store = PhoneLeadsStore(db_path=path)
    _tag_claim_tables(path)
    store.add([
        {**_record("5035550101"), "business_name": "First Business"},
        {**_record("5035550101"), "business_name": "Second Business"},
    ])
    assert store.unclaimed_count("", "WA") == 1
    assert store.servable_by_state() == {"WA": 1}

    first = store.serve("", "WA", "", 2, "alice", tenant_id="tenant-a")

    assert len(first) == 1
    assert store.unclaimed_count("", "WA") == 0
    assert store.servable_by_state() == {}
    assert store.serve("", "WA", "", 1, "bob", tenant_id="tenant-b") == []


def test_unknown_tenant_cannot_claim_phone(tmp_path):
    path = str(tmp_path / "phones.db")
    store = PhoneLeadsStore(db_path=path)
    _tag_claim_tables(path)
    store.add([_record("5035550101")])

    with pytest.raises(ValueError, match="unknown tenant"):
        store.serve("", "WA", "", 1, "alice", tenant_id="missing")
    assert store.unclaimed_count("", "WA") == 1


def test_tenant_mode_claims_fail_closed_without_tenant(tmp_path, monkeypatch):
    path = str(tmp_path / "phones.db")
    store = PhoneLeadsStore(db_path=path)
    _tag_claim_tables(path)
    store.add([_record("5035550101")])
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")

    with pytest.raises(ValueError, match="tenant_id"):
        store.serve("", "WA", "", 1, "alice")
    with pytest.raises(ValueError, match="tenant_id"):
        store.list_owned("alice")
    with pytest.raises(ValueError, match="tenant_id"):
        store.daily_usage("alice")


def test_call_outcomes_and_dates_stay_inside_selected_tenant(tmp_path, monkeypatch):
    path = str(tmp_path / "phones.db")
    store = PhoneLeadsStore(db_path=path)
    _tag_claim_tables(path)
    store.add([_record("5035550101"), _record("5035550102")])
    first = store.serve("", "WA", "", 1, "same-user", tenant_id="tenant-a")[0]
    second = store.serve("", "WA", "", 1, "same-user", tenant_id="tenant-b")[0]
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")

    assert store.record_call_event(first["id"], "same-user", "dialed", tenant_id="tenant-b") is None
    assert store.record_call_event(first["id"], "same-user", "dialed", tenant_id="tenant-a")
    assert store.record_call_event(first["id"], "same-user", "not_interested", tenant_id="tenant-a")
    assert store.record_call_event(second["id"], "same-user", "no_answer", tenant_id="tenant-b")
    activity_a = store.call_activity("same-user", tenant_id="tenant-a")
    activity_b = store.call_activity("same-user", tenant_id="tenant-b")
    assert activity_a["dialed"] == 1
    assert activity_a["outcomes"] == {"not_interested": 1}
    assert {event["lead_id"] for event in activity_a["events"]} == {first["id"]}
    assert activity_b["dialed"] == 0
    assert activity_b["outcomes"] == {"no_answer": 1}
    assert {event["lead_id"] for event in activity_b["events"]} == {second["id"]}
    assert store.call_days("same-user", tenant_id="tenant-a") == [activity_a["date"]]
    assert store.call_days("same-user", tenant_id="tenant-b") == [activity_b["date"]]

    with pytest.raises(ValueError, match="tenant_id"):
        store.record_call_event(first["id"], "same-user", "dialed")
    with pytest.raises(ValueError, match="tenant_id"):
        store.call_activity("same-user")
    with pytest.raises(ValueError, match="tenant_id"):
        store.call_days("same-user")


def test_terminal_phone_actions_and_archive_are_tenant_scoped(tmp_path, monkeypatch):
    source = tmp_path / "phone_leads.db"
    target = tmp_path / "unified.db"
    PhoneLeadsStore(db_path=str(source))
    build_unified_database([source], target)
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO tenants VALUES ('tenant-b', 'Tenant B')")
    store = PhoneLeadsStore(db_path=str(target))
    store.add([
        _record("5035550101"), _record("5035550102"), _record("5035550103"),
    ])
    first = store.serve("", "WA", "", 1, "same-user", tenant_id="the-best-estimators-llc")[0]
    second = store.serve("", "WA", "", 1, "same-user", tenant_id="tenant-b")[0]
    third = store.serve("", "WA", "", 1, "same-user", tenant_id="the-best-estimators-llc")[0]
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")

    assert store.mark_lead(first["id"], "same-user", tenant_id="tenant-b") is None
    assert store.mark_lead(first["id"], "same-user", tenant_id="the-best-estimators-llc")
    assert len(store.list_saved("same-user", kind="lead", tenant_id="the-best-estimators-llc")) == 1
    assert store.list_saved("same-user", kind="lead", tenant_id="tenant-b") == []
    assert store.mark_voicemail(second["id"], "same-user", tenant_id="the-best-estimators-llc") is None
    assert store.mark_voicemail(second["id"], "same-user", tenant_id="tenant-b")
    assert store.call_activity("same-user", tenant_id="tenant-b")["outcomes"] == {"voicemail": 1}
    assert store.mark_wrong_number(third["id"], "same-user", tenant_id="tenant-b") is None
    assert store.mark_wrong_number(third["id"], "same-user", tenant_id="the-best-estimators-llc")
    archive_a = store.list_wrong_archive(tenant_id="the-best-estimators-llc")
    assert len(archive_a) == 1
    assert store.list_wrong_archive(tenant_id="tenant-b") == []
    assert store.recover_wrong_number(archive_a[0]["id"], tenant_id="tenant-b") is False
    assert store.recover_wrong_number(archive_a[0]["id"], tenant_id="the-best-estimators-llc") is True


def test_phone_api_rechecks_membership_and_scopes_sheet(tmp_path, monkeypatch):
    users_source = tmp_path / "users.db"
    phones_source = tmp_path / "phone_leads.db"
    source_users = UserStore(db_path=str(users_source))
    user = source_users.create(
        "caller", "caller@local.test", "safe-password", category="phones",
    )
    admin = source_users.ensure_admin(username="root", password="safe-password")
    only_a = source_users.create(
        "only_a", "only-a@local.test", "safe-password", category="phones",
    )
    PhoneLeadsStore(db_path=str(phones_source))
    target = tmp_path / "unified.db"
    build_unified_database([users_source, phones_source], target)
    users = UserStore(db_path=str(target))
    phones = PhoneLeadsStore(db_path=str(target))
    second_tenant = users.create_tenant("Second Tenant", owner_user_id=admin.id)
    users.grant_membership(second_tenant, user.id, "member")
    phones.add([_record("5035550101"), _record("5035550102")])

    import app.auth.dependencies as dependencies
    import app.auth.activity as activity_module
    import app.api.v1.phones as phones_module

    monkeypatch.setattr(dependencies, "_user_store", lambda: users)
    monkeypatch.setattr(phones_module, "_store", phones)
    activity = ActivityStore(db_path=str(target))
    monkeypatch.setattr(activity_module, "_activity_store", activity)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    app = FastAPI()
    app.include_router(phones_router, prefix="/api/v1")
    from app.api.v1.admin import router as admin_router
    app.include_router(admin_router, prefix="/api/v1")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_access_token(user.id)}"}
    first_id = "the-best-estimators-llc"
    search = "/api/v1/phones/search"
    assert client.post(search, headers=headers, json={"state": "WA", "target": 1}).status_code == 400
    first = client.post(search, headers={**headers, "X-Tenant-ID": first_id}, json={"state": "WA", "target": 1})
    second = client.post(search, headers={**headers, "X-Tenant-ID": second_tenant}, json={"state": "WA", "target": 1})
    assert first.status_code == second.status_code == 200
    assert first.json()["leads"][0]["id"] != second.json()["leads"][0]["id"]
    assert [row["action"] for row in activity.list(tenant_id=first_id)] == ["phone_search"]
    assert [row["action"] for row in activity.list(tenant_id=second_tenant)] == ["phone_search"]
    first_sheet = client.get("/api/v1/phones/leads", headers={**headers, "X-Tenant-ID": first_id})
    second_sheet = client.get("/api/v1/phones/leads", headers={**headers, "X-Tenant-ID": second_tenant})
    assert [lead["id"] for lead in first_sheet.json()] == [first.json()["leads"][0]["id"]]
    assert [lead["id"] for lead in second_sheet.json()] == [second.json()["leads"][0]["id"]]
    assert client.get("/api/v1/phones/stats", headers={**headers, "X-Tenant-ID": first_id}).json()["daily_used"] == 1
    first_lead_id = first.json()["leads"][0]["id"]
    first_headers = {**headers, "X-Tenant-ID": first_id}
    second_headers = {**headers, "X-Tenant-ID": second_tenant}
    admin_headers_a = {
        "Authorization": f"Bearer {create_access_token(admin.id)}",
        "X-Tenant-ID": first_id,
    }
    admin_headers_b = {**admin_headers_a, "X-Tenant-ID": second_tenant}
    event_url = f"/api/v1/phones/leads/{first_lead_id}/event"
    assert client.post(event_url, headers=second_headers, json={"action": "dialed"}).status_code == 404
    assert client.post(event_url, headers=first_headers, json={"action": "dialed"}).status_code == 200
    assert activity.list(tenant_id=first_id)[0]["action"] == "phone_dialed"
    assert activity.list(tenant_id=second_tenant)[0]["action"] == "phone_search"
    assert client.get("/api/v1/phones/activity", headers=first_headers).json()["dialed"] == 1
    assert client.get("/api/v1/phones/activity", headers=second_headers).json()["dialed"] == 0
    assert client.get("/api/v1/phones/activity/days", headers=second_headers).json() == []
    report_a = client.get("/api/v1/admin/phones/claims-report", headers=admin_headers_a)
    report_b = client.get("/api/v1/admin/phones/claims-report", headers=admin_headers_b)
    assert report_a.status_code == report_b.status_code == 200
    assert report_a.json()["total_claims"] == 1
    assert report_b.json()["total_claims"] == 1
    users_b = client.get("/api/v1/admin/users", headers=admin_headers_b)
    assert users_b.status_code == 200
    assert only_a.id not in {row["id"] for row in users_b.json()["users"]}
    limit_url = f"/api/v1/admin/users/{user.id}/phone-limit"
    assert client.put(limit_url, headers=admin_headers_a, json={"daily_limit": 4}).status_code == 200
    assert client.put(limit_url, headers=admin_headers_b, json={"daily_limit": 5}).status_code == 200
    assert client.get("/api/v1/phones/stats", headers=first_headers).json()["daily_limit"] == 4
    assert client.get("/api/v1/phones/stats", headers=second_headers).json()["daily_limit"] == 5
    assert client.post(
        f"/api/v1/phones/leads/{first_lead_id}/store", headers=second_headers,
    ).status_code == 404
    saved_a = client.post(
        f"/api/v1/phones/leads/{first_lead_id}/store", headers=first_headers,
    )
    assert saved_a.status_code == 200
    saved_id = saved_a.json()["saved_id"]
    assert [row["id"] for row in client.get(
        "/api/v1/phones/saved", headers=first_headers,
    ).json()] == [saved_id]
    assert client.get("/api/v1/phones/saved", headers=second_headers).json() == []
    note_url = f"/api/v1/phones/saved/{saved_id}/note"
    assert client.put(note_url, headers=second_headers, json={"note": "steal"}).status_code == 404
    assert client.delete(f"/api/v1/phones/saved/{saved_id}", headers=second_headers).status_code == 404
    assert client.put(note_url, headers=first_headers, json={"note": "call tomorrow"}).status_code == 200
    assert client.get("/api/v1/phones/saved", headers=first_headers).json()[0]["note"] == "call tomorrow"
    second_lead_id = second.json()["leads"][0]["id"]
    assert client.post(
        f"/api/v1/phones/leads/{second_lead_id}/note", headers=second_headers,
        json={"note": "tenant B note"},
    ).status_code == 200
    assert client.get("/api/v1/phones/saved", headers=first_headers).json()[0]["note"] == "call tomorrow"
    assert len(client.get("/api/v1/phones/saved", headers=second_headers).json()) == 1
    lead_url = f"/api/v1/phones/leads/{first_lead_id}/lead"
    assert client.post(lead_url, headers=second_headers).status_code == 404
    assert client.post(lead_url, headers=first_headers).status_code == 200
    assert len(client.get("/api/v1/phones/saved?kind=lead", headers=first_headers).json()) == 1
    voicemail_url = f"/api/v1/phones/leads/{second_lead_id}/voicemail"
    assert client.post(voicemail_url, headers=first_headers).status_code == 404
    assert client.post(voicemail_url, headers=second_headers).status_code == 200
    phones.add([_record("5035550103")])
    third = client.post(search, headers=first_headers, json={"state": "WA", "target": 1})
    assert third.status_code == 200
    wrong_url = f"/api/v1/phones/leads/{third.json()['leads'][0]['id']}/event"
    assert client.post(wrong_url, headers=second_headers, json={"action": "wrong_number"}).status_code == 404
    assert client.post(wrong_url, headers=first_headers, json={"action": "wrong_number"}).status_code == 200
    archive_a = client.get("/api/v1/admin/phones/wrong", headers=admin_headers_a)
    archive_b = client.get("/api/v1/admin/phones/wrong", headers=admin_headers_b)
    assert archive_a.status_code == archive_b.status_code == 200
    assert len(archive_a.json()) == 1
    assert archive_b.json() == []
    recover_url = f"/api/v1/admin/phones/wrong/{archive_a.json()[0]['id']}/recover"
    assert client.post(recover_url, headers=admin_headers_b).status_code == 404
    assert client.post(recover_url, headers=admin_headers_a).status_code == 200
    users.revoke_membership(second_tenant, user.id)
    assert client.get("/api/v1/phones/leads", headers={**headers, "X-Tenant-ID": second_tenant}).status_code == 403
    assert client.get("/api/v1/phones/saved", headers=second_headers).status_code == 403
