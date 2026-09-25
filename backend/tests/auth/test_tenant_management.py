"""Platform admin can manage tenants without leaving ownerless workspaces."""

from fastapi.testclient import TestClient
import pytest

import app.auth.dependencies as dependencies
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from app.main import app
from scripts.prepare_unified_database import build_unified_database


def _setup(tmp_path, monkeypatch):
    source = tmp_path / "users.db"
    original = UserStore(db_path=str(source))
    admin = original.ensure_admin()
    member = original.create("caller", "caller@local.test", "safe-password")
    target = tmp_path / "unified.db"
    build_unified_database([source], target)
    store = UserStore(db_path=str(target))
    monkeypatch.setattr(dependencies, "_user_store", lambda: store)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    client = TestClient(app)
    admin_headers = {"Authorization": f"Bearer {create_access_token(admin.id)}"}
    member_headers = {"Authorization": f"Bearer {create_access_token(member.id)}"}
    return client, store, admin, member, admin_headers, member_headers


def test_admin_creates_tenant_as_owner_and_sees_member_counts(tmp_path, monkeypatch):
    client, store, admin, member, headers, member_headers = _setup(tmp_path, monkeypatch)

    assert client.post("/api/v1/admin/tenants", json={"name": "Nope"},
                       headers=member_headers).status_code == 403
    response = client.post("/api/v1/admin/tenants", json={"name": "Second Firm"},
                           headers=headers)
    assert response.status_code == 201, response.text
    tenant_id = response.json()["id"]
    assert response.json()["member_count"] == 1
    assert store.tenant_role(admin.id, tenant_id) == "owner"
    assert store.tenant_role(member.id, tenant_id) is None

    listed = client.get("/api/v1/admin/tenants", headers=headers)
    assert listed.status_code == 200
    assert [(t["name"], t["member_count"]) for t in listed.json()] == [
        ("Second Firm", 1), ("The Best Estimators LLC", 2),
    ]
    assert client.post("/api/v1/admin/tenants", json={"name": "Second Firm"},
                       headers=headers).status_code == 409
    assert len(client.get("/api/v1/admin/tenants", headers=headers).json()) == 2


def test_admin_assigns_existing_user_and_cannot_remove_last_owner(tmp_path, monkeypatch):
    client, store, admin, member, headers, _ = _setup(tmp_path, monkeypatch)
    tenant_id = client.post("/api/v1/admin/tenants", json={"name": "Second Firm"},
                            headers=headers).json()["id"]
    add = client.put(f"/api/v1/admin/tenants/{tenant_id}/members/{member.id}",
                     headers=headers)
    assert add.status_code == 200
    assert store.tenant_role(member.id, tenant_id) == "member"
    listed = client.get(f"/api/v1/admin/tenants/{tenant_id}/members", headers=headers)
    assert [(row["user_id"], row["role"]) for row in listed.json()] == [
        (admin.id, "owner"), (member.id, "member"),
    ]
    assert client.delete(f"/api/v1/admin/tenants/{tenant_id}/members/{admin.id}",
                         headers=headers).status_code == 409
    assert client.put(f"/api/v1/admin/tenants/{tenant_id}/members/{admin.id}",
                      headers=headers).status_code == 409
    assert store.tenant_role(admin.id, tenant_id) == "owner"
    assert client.delete(f"/api/v1/admin/tenants/{tenant_id}/members/{member.id}",
                         headers=headers).status_code == 200
    assert store.tenant_role(member.id, tenant_id) is None
    assert client.delete(f"/api/v1/admin/tenants/{tenant_id}/members/{member.id}",
                         headers=headers).status_code == 404
    assert client.put(f"/api/v1/admin/tenants/{tenant_id}/members/missing",
                      headers=headers).status_code == 404


def test_store_rejects_demoting_the_only_owner(tmp_path, monkeypatch):
    _, store, admin, _, _, _ = _setup(tmp_path, monkeypatch)
    tenant_id = store.create_tenant("Second Firm", owner_user_id=admin.id)
    with pytest.raises(ValueError, match="last tenant owner"):
        store.grant_membership(tenant_id, admin.id, "member")
    assert store.tenant_role(admin.id, tenant_id) == "owner"


def test_tenant_management_hidden_in_legacy_mode(tmp_path, monkeypatch):
    client, _, _, _, headers, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("LEADHUNTER_MULTI_TENANT_ENABLED")
    assert client.get("/api/v1/admin/tenants", headers=headers).status_code == 404
