"""Login-auth ON/OFF toggle — the open-site mode.

Covers: the public /auth/mode boot check, the admin-only toggle, the shared
anonymous identity while OFF (never admin), admin sessions surviving the
toggle, anonymous users being locked out of admin endpoints, and the
shared-account delete guard.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.api.v1.auth as auth_module
import app.auth.activity as activity_module
import app.auth.dependencies as deps
import app.auth.settings as settings_module
from app.auth.activity import ActivityStore
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from app.auth.settings import AuthSettings
from app.main import app


def _setup(tmp_path, monkeypatch):
    """tmp users.db (accounts + activity + auth settings), auth ON by default.
    Returns (admin_client, user_store, auth_settings, admin)."""
    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    monkeypatch.setattr(auth_module, "_store", user_store)
    activity = ActivityStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(activity_module, "_activity_store", activity)
    auth_settings = AuthSettings(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(settings_module, "_instance", auth_settings)
    admin = user_store.ensure_admin()
    token = create_access_token(admin.id, admin.is_admin, username=admin.username)
    admin_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    return admin_client, user_store, auth_settings, admin


# ---------------------------------------------------------------------------
# The public boot check
# ---------------------------------------------------------------------------

def test_mode_defaults_to_auth_on(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    r = TestClient(app).get("/api/v1/auth/mode")

    assert r.status_code == 200
    assert r.json() == {"auth_enabled": True}


def test_settings_persist_across_instances(tmp_path):
    s = AuthSettings(db_path=str(tmp_path / "users.db"))
    assert s.auth_enabled() is True  # missing row = safe default

    s.set_auth_enabled(False)
    assert s.auth_enabled() is False

    # A fresh instance reads the SAME persisted value, not the default.
    assert AuthSettings(db_path=str(tmp_path / "users.db")).auth_enabled() is False


# ---------------------------------------------------------------------------
# The admin toggle
# ---------------------------------------------------------------------------

def test_admin_toggles_auth_off_and_on(tmp_path, monkeypatch):
    admin_client, _, _, _ = _setup(tmp_path, monkeypatch)

    r = admin_client.post("/api/v1/admin/auth-mode", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"auth_enabled": False}
    assert TestClient(app).get("/api/v1/auth/mode").json() == {"auth_enabled": False}

    r = admin_client.post("/api/v1/admin/auth-mode", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"auth_enabled": True}
    assert TestClient(app).get("/api/v1/auth/mode").json() == {"auth_enabled": True}


def test_toggle_requires_authentication(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    # No token while auth is ON → 401, never a silent toggle.
    r = TestClient(app).post("/api/v1/admin/auth-mode", json={"enabled": False})

    assert r.status_code == 401


# ---------------------------------------------------------------------------
# The shared anonymous identity
# ---------------------------------------------------------------------------

def test_anonymous_is_401_when_on_and_shared_when_off(tmp_path, monkeypatch):
    admin_client, user_store, _, _ = _setup(tmp_path, monkeypatch)
    anon = TestClient(app)

    # Auth ON: no token → 401 (the login wall).
    assert anon.get("/api/v1/auth/me").status_code == 401

    # Auth OFF: the same token-less request runs as the shared account.
    admin_client.post("/api/v1/admin/auth-mode", json={"enabled": False})
    r = anon.get("/api/v1/auth/me")
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["username"] == "shared"
    assert me["is_admin"] is False

    # The shared account exists in the store and is a REAL non-admin user.
    shared = user_store.get_by_username("shared")
    assert shared is not None
    assert shared.is_admin is False


def test_admin_token_survives_the_toggle(tmp_path, monkeypatch):
    admin_client, _, _, admin = _setup(tmp_path, monkeypatch)

    admin_client.post("/api/v1/admin/auth-mode", json={"enabled": False})
    r = admin_client.get("/api/v1/auth/me")

    assert r.status_code == 200
    assert r.json()["username"] == admin.username
    assert r.json()["is_admin"] is True


def test_anonymous_cannot_reach_admin_endpoints_while_off(tmp_path, monkeypatch):
    """Open site ≠ open admin. The shared account is a normal user: admin
    routes stay 403 for token-less visitors."""
    admin_client, _, _, _ = _setup(tmp_path, monkeypatch)
    admin_client.post("/api/v1/admin/auth-mode", json={"enabled": False})

    r = TestClient(app).post("/api/v1/admin/auth-mode", json={"enabled": True})

    assert r.status_code == 403


def test_shared_account_cannot_be_deleted(tmp_path, monkeypatch):
    """The shared account is the anonymous identity — deleting it would
    orphan every open-site action, so the endpoint refuses."""
    admin_client, user_store, _, _ = _setup(tmp_path, monkeypatch)
    shared = user_store.ensure_shared()

    r = admin_client.delete(f"/api/v1/admin/users/{shared.id}")

    assert r.status_code == 422
    assert user_store.get_by_username("shared") is not None


def test_ensure_shared_is_idempotent_and_never_admin(tmp_path):
    store = UserStore(db_path=str(tmp_path / "users.db"))

    first = store.ensure_shared()
    second = store.ensure_shared()

    assert first.id == second.id
    assert first.is_admin is False


# ---------------------------------------------------------------------------
# The Gmail-inbox interface toggle (browse off, export stays on)
# ---------------------------------------------------------------------------

def test_gmail_inbox_toggle_defaults_on(tmp_path):
    s = AuthSettings(db_path=str(tmp_path / "users.db"))
    assert s.gmail_inbox_enabled() is True  # missing row = feature ON

    s.set_gmail_inbox_enabled(False)
    assert s.gmail_inbox_enabled() is False
    # A fresh instance reads the persisted value, not the default.
    assert AuthSettings(db_path=str(tmp_path / "users.db")).gmail_inbox_enabled() is False


def test_admin_toggles_gmail_inbox_off_and_on(tmp_path, monkeypatch):
    admin_client, _, auth_settings, _ = _setup(tmp_path, monkeypatch)

    r = admin_client.post("/api/v1/admin/gmail-inbox-mode",
                          json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"inbox_enabled": False}
    assert auth_settings.gmail_inbox_enabled() is False

    r = admin_client.post("/api/v1/admin/gmail-inbox-mode",
                          json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"inbox_enabled": True}
    assert auth_settings.gmail_inbox_enabled() is True


def test_gmail_inbox_toggle_requires_admin(tmp_path, monkeypatch):
    admin_client, user_store, _, _ = _setup(tmp_path, monkeypatch)
    user = user_store.create("normal", "n@x.com", "pw")
    token = create_access_token(user.id, user.is_admin, username=user.username)
    user_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})

    r = user_client.post("/api/v1/admin/gmail-inbox-mode",
                         json={"enabled": False})

    assert r.status_code == 403  # a normal user can never flip it
