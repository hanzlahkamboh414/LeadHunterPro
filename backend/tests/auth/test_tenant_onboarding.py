"""Tenant-mode onboarding must bind accounts to one authorized workspace."""

from fastapi.testclient import TestClient

import app.api.v1.auth as auth_module
import app.auth.activity as activity_module
import app.auth.dependencies as dependencies
from app.auth.activity import ActivityStore
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from app.main import app
from scripts.prepare_unified_database import build_unified_database


def _setup(tmp_path, monkeypatch):
    source = tmp_path / "users.db"
    original = UserStore(db_path=str(source))
    admin = original.ensure_admin()
    outsider = original.create("outsider", "outsider@local.test", "safe-password")
    target = tmp_path / "unified.db"
    build_unified_database([source], target)
    store = UserStore(db_path=str(target))
    second = store.create_tenant("Second Firm")
    monkeypatch.setattr(dependencies, "_user_store", lambda: store)
    monkeypatch.setattr(auth_module, "_store", store)
    monkeypatch.setattr(
        activity_module, "_activity_store", ActivityStore(db_path=str(target)),
    )
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    admin_token = create_access_token(admin.id, is_admin=True)
    outsider_token = create_access_token(outsider.id)
    return TestClient(app), store, second, admin_token, outsider_token


def test_admin_creation_requires_valid_tenant_and_grants_only_that_membership(
    tmp_path, monkeypatch,
):
    client, store, second, token, _ = _setup(tmp_path, monkeypatch)
    payload = {
        "username": "new_caller", "email": "new@local.test",
        "password": "safe-password",
    }
    headers = {"Authorization": f"Bearer {token}"}

    assert client.post("/api/v1/admin/users", json=payload, headers=headers).status_code == 422
    assert client.post(
        "/api/v1/admin/users", json={**payload, "tenant_id": "missing"},
        headers=headers,
    ).status_code == 422
    assert store.get_by_username("new_caller") is None

    response = client.post(
        "/api/v1/admin/users", json={**payload, "tenant_id": second},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]
    assert store.tenant_role(user_id, second) == "member"
    assert store.tenant_role(user_id, "the-best-estimators-llc") is None

    user_headers = {"Authorization": f"Bearer {create_access_token(user_id)}"}
    assert client.get("/api/v1/auth/tenants", headers=user_headers).json() == [
        {"id": second, "name": "Second Firm", "role": "member"},
    ]


def test_tenant_list_requires_auth_and_never_exposes_another_membership(
    tmp_path, monkeypatch,
):
    client, store, second, _, outsider_token = _setup(tmp_path, monkeypatch)
    assert client.get("/api/v1/auth/tenants").status_code == 401
    headers = {"Authorization": f"Bearer {outsider_token}"}
    assert client.get("/api/v1/auth/tenants", headers=headers).json() == [
        {"id": "the-best-estimators-llc", "name": "The Best Estimators LLC", "role": "member"},
    ]
    outsider = store.get_by_username("outsider")
    store.grant_membership(second, outsider.id, "admin")
    assert len(client.get("/api/v1/auth/tenants", headers=headers).json()) == 2
    store.revoke_membership(second, outsider.id)
    assert len(client.get("/api/v1/auth/tenants", headers=headers).json()) == 1
