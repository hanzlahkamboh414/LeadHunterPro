"""Tenant activity is attributed and never listed across workspaces."""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.admin import router as admin_router
from app.auth.activity import ActivityStore
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from scripts.prepare_unified_database import build_unified_database


def test_historical_activity_maps_to_first_tenant_and_new_rows_are_separate(
    tmp_path, monkeypatch,
):
    source = tmp_path / "users.db"
    target = tmp_path / "unified.db"
    user = UserStore(db_path=str(source)).create(
        "caller", "caller@example.com", "secret123",
    )
    old_activity = ActivityStore(db_path=str(source))
    old_activity.record(user.id, user.username, "search", "old search")
    build_unified_database([source], target)
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT tenant_id FROM activity_log WHERE action = 'search'"
        ).fetchall() == [("the-best-estimators-llc",)]
        conn.execute("INSERT INTO tenants VALUES ('tenant-b', 'Tenant B')")
    activity = ActivityStore(db_path=str(target))
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")

    activity.record(user.id, user.username, "linkedin_search", "A", tenant_id="the-best-estimators-llc")
    activity.record(user.id, user.username, "linkedin_search", "B", tenant_id="tenant-b")
    first = activity.list(tenant_id="the-best-estimators-llc")
    second = activity.list(tenant_id="tenant-b")
    assert {row["detail"] for row in first} == {"A", "old search"}
    assert {row["detail"] for row in second} == {"B"}
    with pytest.raises(ValueError, match="tenant_id"):
        activity.list()
    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT COUNT(*) FROM activity_log").fetchone() == (1,)


def test_old_activity_schema_without_tenant_column_migrates_offline(tmp_path):
    source = tmp_path / "old_users.db"
    target = tmp_path / "unified.db"
    with sqlite3.connect(source) as conn:
        conn.execute(
            "CREATE TABLE activity_log (user_id TEXT NOT NULL, action TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO activity_log VALUES ('caller', 'search')"
        )

    build_unified_database([source], target)

    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT tenant_id FROM activity_log"
        ).fetchall() == [("the-best-estimators-llc",)]
    with sqlite3.connect(source) as conn:
        assert "tenant_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(activity_log)")
        }


def test_admin_activity_api_only_shows_selected_membership(tmp_path, monkeypatch):
    source = tmp_path / "users.db"
    target = tmp_path / "unified.db"
    original = UserStore(db_path=str(source))
    admin = original.ensure_admin(username="admin", password="safe-password")
    ActivityStore(db_path=str(source))
    build_unified_database([source], target)
    users = UserStore(db_path=str(target))
    second = users.create_tenant("Tenant B", owner_user_id=admin.id)
    activity = ActivityStore(db_path=str(target))
    activity.record(admin.id, admin.username, "test", "A", "the-best-estimators-llc")
    activity.record(admin.id, admin.username, "test", "B", second)

    import app.auth.activity as activity_module
    import app.auth.dependencies as dependencies

    monkeypatch.setattr(activity_module, "_activity_store", activity)
    monkeypatch.setattr(dependencies, "_user_store", lambda: users)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {create_access_token(admin.id)}"}
    url = "/api/v1/admin/activity"

    assert client.get(url, headers=auth).status_code == 400
    first = client.get(
        url, headers={**auth, "X-Tenant-ID": "the-best-estimators-llc"},
    )
    assert [row["detail"] for row in first.json()["activity"]] == ["A"]
    second_view = client.get(url, headers={**auth, "X-Tenant-ID": second})
    assert [row["detail"] for row in second_view.json()["activity"]] == ["B"]
    assert client.get(
        url, headers={**auth, "X-Tenant-ID": "not-a-member"},
    ).status_code == 403
