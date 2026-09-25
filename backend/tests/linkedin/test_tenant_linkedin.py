"""LinkedIn byproduct inventory stays shared; ownership stays tenant-private."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.linkedin import router as linkedin_router
from app.auth.activity import ActivityStore
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from app.linkedin.store import LinkedInLeadsStore
from scripts.prepare_unified_database import build_unified_database


def _stock(store):
    store.add([
        {
            "person_name": "Jane Smith", "role": "Owner",
            "linkedin_url": "https://www.linkedin.com/in/jane-smith",
            "company_name": "Acme GC", "trade": "General Contractor",
            "location": "Vancouver, WA",
        },
        {
            "person_name": "Alex Smith", "role": "Owner",
            "linkedin_url": "https://www.linkedin.com/in/alex-smith",
            "company_name": "Second GC", "trade": "General Contractor",
            "location": "Vancouver, WA",
        },
    ])


def _unified(tmp_path):
    users_source = tmp_path / "users.db"
    linkedin_source = tmp_path / "linkedin.db"
    users = UserStore(db_path=str(users_source))
    user = users.create("shared", "shared@example.com", "secret123")
    users.ensure_admin(username="admin", password="safe-password")
    linkedin = LinkedInLeadsStore(db_path=str(linkedin_source))
    _stock(linkedin)
    first = linkedin.serve("gc", "WA", "", 1, user.id)[0]
    target = tmp_path / "unified.db"
    build_unified_database([users_source, linkedin_source], target)
    return user, first, target, linkedin_source


def test_offline_linkedin_claims_require_tenant_without_changing_source(tmp_path):
    user, first, target, source = _unified(tmp_path)
    with sqlite3.connect(target) as conn:
        columns = {row[1]: row for row in conn.execute(
            "PRAGMA table_info(linkedin_lead_owners)"
        )}
        assert columns["tenant_id"][3] == 1
        assert columns["tenant_id"][4] is None
        assert conn.execute(
            "SELECT lead_id, user_id, tenant_id FROM linkedin_lead_owners"
        ).fetchall() == [(first["id"], user.id, "the-best-estimators-llc")]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO linkedin_lead_owners (lead_id, user_id, created_at) "
                "VALUES (?, ?, 'now')",
                (first["id"], "new-user"),
            )
    with sqlite3.connect(source) as conn:
        assert "tenant_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(linkedin_lead_owners)")
        }


def test_same_user_cannot_move_linkedin_claim_between_tenants(tmp_path, monkeypatch):
    user, first, target, _ = _unified(tmp_path)
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO tenants VALUES ('tenant-b', 'Tenant B')")
    store = LinkedInLeadsStore(db_path=str(target))
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")

    assert [row["id"] for row in store.list_owned(
        user.id, tenant_id="the-best-estimators-llc",
    )] == [first["id"]]
    assert store.list_owned(user.id, tenant_id="tenant-b") == []
    served = store.serve("gc", "WA", "", 2, user.id, tenant_id="tenant-b")
    assert len(served) == 1
    assert served[0]["id"] != first["id"]
    assert store.serve("gc", "WA", "", 2, user.id, tenant_id="tenant-b") == served
    with pytest.raises(ValueError, match="tenant_id"):
        store.list_owned(user.id)
    with pytest.raises(ValueError, match="unknown tenant_id"):
        store.serve("gc", "WA", "", 1, user.id, tenant_id="missing")


def test_unknown_linkedin_owner_column_aborts_offline_publication(tmp_path):
    source = tmp_path / "linkedin.db"
    target = tmp_path / "unified.db"
    LinkedInLeadsStore(db_path=str(source))
    with sqlite3.connect(source) as conn:
        conn.execute(
            "ALTER TABLE linkedin_lead_owners ADD COLUMN future_private TEXT"
        )
    with pytest.raises(ValueError, match="linkedin_lead_owners.*columns"):
        build_unified_database([source], target)
    assert not target.exists()


def test_simultaneous_linkedin_claim_is_globally_exclusive(tmp_path, monkeypatch):
    _, _, target, _ = _unified(tmp_path)
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO tenants VALUES ('tenant-b', 'Tenant B')")
    store = LinkedInLeadsStore(db_path=str(target))
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    start = Barrier(2)

    def claim(tenant_id):
        start.wait()
        return store.serve("gc", "WA", "", 1, "other-user", tenant_id=tenant_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(claim, "the-best-estimators-llc")
        second = executor.submit(claim, "tenant-b")
        results = (first.result(), second.result())

    assert sorted(len(rows) for rows in results) == [0, 1]


def test_linkedin_api_rechecks_live_membership(tmp_path, monkeypatch):
    user, first, target, _ = _unified(tmp_path)
    users = UserStore(db_path=str(target))
    admin = users.get_by_username("admin")
    second = users.create_tenant("Tenant B", owner_user_id=admin.id)
    users.grant_membership(second, user.id, "member")
    store = LinkedInLeadsStore(db_path=str(target))
    import app.auth.dependencies as dependencies
    import app.api.v1.linkedin as linkedin_module
    import app.auth.activity as activity_module

    monkeypatch.setattr(dependencies, "_user_store", lambda: users)
    monkeypatch.setattr(linkedin_module, "_store", store)
    activity = ActivityStore(db_path=str(target))
    monkeypatch.setattr(activity_module, "_activity_store", activity)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    app = FastAPI()
    app.include_router(linkedin_router, prefix="/api/v1")
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {create_access_token(user.id)}"}
    url = "/api/v1/linkedin/leads"

    assert client.get(url, headers=auth).status_code == 400
    assert client.post(
        "/api/v1/linkedin/search", headers=auth, json={"target": 1},
    ).status_code == 400
    assert [row["id"] for row in client.get(
        url, headers={**auth, "X-Tenant-ID": "the-best-estimators-llc"},
    ).json()] == [first["id"]]
    assert client.get(url, headers={**auth, "X-Tenant-ID": second}).json() == []
    search = client.post(
        "/api/v1/linkedin/search",
        headers={**auth, "X-Tenant-ID": second},
        json={"trade": "GC", "state": "WA", "target": 1},
    )
    assert search.status_code == 200
    assert len(search.json()["leads"]) == 1
    assert [row["action"] for row in activity.list(tenant_id=second)] == [
        "linkedin_search",
    ]
    assert activity.list(tenant_id="the-best-estimators-llc") == []
    users.revoke_membership(second, user.id)
    assert client.get(url, headers={**auth, "X-Tenant-ID": second}).status_code == 403
    assert client.get(
        "/api/v1/linkedin/stats", headers={**auth, "X-Tenant-ID": second},
    ).status_code == 403
