"""Multi-tenant mode must not inherit public account takeover paths."""

import os
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

import app.api.v1.auth as auth_module
import app.auth.activity as activity_module
import app.auth.dependencies as dependencies
import app.auth.settings as auth_settings_module
from app.auth.activity import ActivityStore
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from app.auth.settings import AuthSettings
from app.main import app, verify_tenant_startup
from scripts.prepare_unified_database import build_unified_database


def _setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "users.db")
    store = UserStore(db_path=db_path)
    user = store.create("caller", "caller@local.test", "original-password")
    admin = store.ensure_admin()
    monkeypatch.setattr(auth_module, "_store", store)
    monkeypatch.setattr(dependencies, "_user_store", lambda: store)
    monkeypatch.setattr(activity_module, "_activity_store", ActivityStore(db_path=db_path))
    settings = AuthSettings(db_path=db_path)
    monkeypatch.setattr(auth_settings_module, "_instance", settings)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    admin_token = create_access_token(admin.id, is_admin=True)
    return TestClient(app), store, user, settings, admin_token


def test_public_signup_and_unverified_reset_are_disabled(tmp_path, monkeypatch):
    client, store, user, _, _ = _setup(tmp_path, monkeypatch)

    assert client.post("/api/v1/auth/signup", json={
        "username": "outsider", "email": "outsider@local.test", "password": "pass123",
    }).status_code == 403
    assert store.get_by_username("outsider") is None

    assert client.post("/api/v1/auth/reset-password", json={
        "username": user.username, "new_password": "hijacked-password",
    }).status_code == 403
    assert store.verify_password(user.username, "original-password") is not None
    assert store.verify_password(user.username, "hijacked-password") is None


def test_login_wall_cannot_be_disabled_in_tenant_mode(tmp_path, monkeypatch):
    client, _, _, settings, admin_token = _setup(tmp_path, monkeypatch)
    settings.set_auth_enabled(False)

    assert client.get("/api/v1/auth/mode").json() == {
        "auth_enabled": True, "tenant_mode": True,
    }
    response = client.post(
        "/api/v1/admin/auth-mode",
        json={"enabled": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 409


def test_tenant_boot_rejects_factory_admin_password(tmp_path):
    source = tmp_path / "users.db"
    store = UserStore(db_path=str(source))

    with pytest.raises(RuntimeError, match="platform admin"):
        store.require_secure_platform_admin()
    store.ensure_admin()
    target = tmp_path / "unified.db"
    build_unified_database([source], target)
    store = UserStore(db_path=str(target))
    with pytest.raises(RuntimeError, match="factory password"):
        store.require_secure_platform_admin()
    store.reset_password("admin4269", "a-long-new-private-password")
    store.require_secure_platform_admin()


def test_app_refuses_tenant_mode_without_unified_database(monkeypatch):
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    monkeypatch.delenv("LEADHUNTER_UNIFIED_DB_PATH", raising=False)

    with pytest.raises(RuntimeError, match="requires a unified database"):
        verify_tenant_startup()


def test_app_refuses_unisolated_version_one_copy(tmp_path, monkeypatch):
    source = tmp_path / "users.db"
    original = UserStore(db_path=str(source))
    admin = original.ensure_admin()
    original.reset_password(admin.username, "a-long-new-private-password")
    target = tmp_path / "unified.db"
    build_unified_database([source], target)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    monkeypatch.setenv("LEADHUNTER_UNIFIED_DB_PATH", str(target))

    with pytest.raises(RuntimeError, match="tenant isolation schema not ready"):
        verify_tenant_startup()

    with sqlite3.connect(target) as conn:
        before = conn.execute(
            "SELECT name FROM sqlite_master ORDER BY name"
        ).fetchall()
    env = os.environ.copy()
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "tenant isolation schema not ready" in result.stderr
    with sqlite3.connect(target) as conn:
        after = conn.execute(
            "SELECT name FROM sqlite_master ORDER BY name"
        ).fetchall()
    assert after == before
