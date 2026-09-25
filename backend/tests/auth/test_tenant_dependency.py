"""A tenant selector is never authorization; membership is checked live."""

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import app.auth.dependencies as dependencies
import app.auth.settings as auth_settings_module
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from app.auth.settings import AuthSettings
from scripts.prepare_unified_database import build_unified_database


def _client(tmp_path, monkeypatch):
    source = tmp_path / "users.db"
    initial = UserStore(db_path=str(source))
    user = initial.create("tenant_caller", "caller@local.test", "safe-password")
    target = tmp_path / "unified.db"
    build_unified_database([source], target)
    store = UserStore(db_path=str(target))
    monkeypatch.setattr(dependencies, "_user_store", lambda: store)
    auth_settings = AuthSettings(db_path=str(target))
    monkeypatch.setattr(auth_settings_module, "_instance", auth_settings)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")

    app = FastAPI()

    @app.get("/private")
    def private(context=Depends(dependencies.get_tenant_context)):
        return {"tenant_id": context.tenant_id, "role": context.role}

    token = create_access_token(user.id)
    return TestClient(app), store, user, token, auth_settings


def test_selector_must_match_live_membership(tmp_path, monkeypatch):
    client, store, user, token, _ = _client(tmp_path, monkeypatch)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Tenant-ID": "the-best-estimators-llc",
    }

    response = client.get("/private", headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "the-best-estimators-llc", "role": "member",
    }
    assert client.get("/private", headers={
        **headers, "X-Tenant-ID": "another-tenant",
    }).status_code == 403
    assert client.get("/private", headers={
        "Authorization": f"Bearer {token}",
    }).status_code == 400

    store.revoke_membership("the-best-estimators-llc", user.id)
    assert client.get("/private", headers=headers).status_code == 403


def test_auth_off_cannot_create_anonymous_multi_tenant_session(tmp_path, monkeypatch):
    client, _, _, token, auth_settings = _client(tmp_path, monkeypatch)
    auth_settings.set_auth_enabled(False)

    assert client.get("/private", headers={
        "X-Tenant-ID": "the-best-estimators-llc",
    }).status_code == 401
    assert client.get("/private", headers={
        "Authorization": f"Bearer {token}",
        "X-Tenant-ID": "the-best-estimators-llc",
    }).status_code == 200
