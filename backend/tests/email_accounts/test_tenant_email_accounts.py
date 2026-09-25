"""Tenant ownership of encrypted Gmail credentials and offline migration."""

import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import app.api.v1.email_accounts as ea
import app.api.v1.gmail_inbox as gi
import app.auth.dependencies as deps
from app.auth.activity import ActivityStore
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from app.email_accounts import google
from app.email_accounts.store import EmailAccountStore
from app.main import app
from scripts.prepare_unified_database import build_unified_database


def _tenant_copy(tmp_path):
    source = tmp_path / "users.db"
    store = EmailAccountStore(str(source))
    store.connect("shared-user", "same@gmail.com", access_token="secret-a")
    target = tmp_path / "unified.db"
    build_unified_database([source], target)
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES ('other', 'Other')")
    return target


def test_offline_copy_maps_credentials_without_changing_source(tmp_path):
    target = _tenant_copy(tmp_path)
    with sqlite3.connect(target) as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(email_accounts)")}
        assert columns["tenant_id"][3] == 1
        assert columns["tenant_id"][4] is None
        assert conn.execute(
            "SELECT tenant_id, access_token FROM email_accounts"
        ).fetchone()[0] == "the-best-estimators-llc"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO email_accounts (user_id, email) "
                "VALUES ('shared-user', 'no-tenant@gmail.com')"
            )
    with sqlite3.connect(tmp_path / "users.db") as conn:
        assert "tenant_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(email_accounts)")
        }


def test_same_user_accounts_are_isolated_across_tenants(tmp_path, monkeypatch):
    target = _tenant_copy(tmp_path)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    store = EmailAccountStore(str(target))
    first = "the-best-estimators-llc"
    a = store.list_for_user("shared-user", tenant_id=first)[0]["id"]
    assert store.list_for_user("shared-user", tenant_id="other") == []
    assert store.get_credentials(a, "shared-user", tenant_id="other") is None
    assert store.mark_status(a, "shared-user", "revoked", tenant_id="other") is False
    assert store.update_tokens(
        a, "shared-user", access_token="bad", token_expires_at="", tenant_id="other"
    ) is False
    assert store.delete(a, "shared-user", tenant_id="other") is False

    store.connect("shared-user", "same@gmail.com", access_token="secret-b", tenant_id="other")
    b = store.list_for_user("shared-user", tenant_id="other")[0]["id"]
    assert b != a
    assert store.get_credentials(a, "shared-user", tenant_id=first)["access_token"] == "secret-a"
    assert store.get_credentials(b, "shared-user", tenant_id="other")["access_token"] == "secret-b"
    assert store.delete(a, "shared-user", tenant_id=first) is True
    assert len(store.list_for_user("shared-user", tenant_id="other")) == 1


def test_missing_or_unknown_tenant_fails_closed(tmp_path, monkeypatch):
    target = _tenant_copy(tmp_path)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    store = EmailAccountStore(str(target))
    with pytest.raises(ValueError, match="tenant_id is required"):
        store.list_for_user("shared-user")
    with pytest.raises(ValueError, match="unknown tenant_id"):
        store.connect("shared-user", "new@gmail.com", tenant_id="invented")


def test_unclassified_email_account_column_aborts_publication(tmp_path):
    source = tmp_path / "users.db"
    store = EmailAccountStore(str(source))
    store.connect("u", "a@gmail.com", access_token="secret")
    with sqlite3.connect(source) as conn:
        conn.execute("ALTER TABLE email_accounts ADD COLUMN custom_secret TEXT")
    target = tmp_path / "unified.db"
    with pytest.raises(ValueError, match="email_accounts has unrecognized columns"):
        build_unified_database([source], target)
    assert not target.exists()


def test_pending_oauth_nonces_are_not_copied_into_new_workspace(tmp_path):
    source = tmp_path / "users.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE email_oauth_pending (nonce_hash TEXT PRIMARY KEY)")
    target = tmp_path / "unified.db"
    with pytest.raises(ValueError, match="reserved unified table"):
        build_unified_database([source], target)
    assert not target.exists()


def _tenant_api(tmp_path, monkeypatch):
    source = tmp_path / "users.db"
    original = UserStore(str(source))
    user = original.create("caller", "caller@example.com", "password")
    EmailAccountStore(str(source))
    target = tmp_path / "unified.db"
    build_unified_database([source], target)
    users = UserStore(str(target))
    other = users.create_tenant("Other")
    users.grant_membership(other, user.id, "member")
    accounts = EmailAccountStore(str(target))
    activity = ActivityStore(str(target))
    monkeypatch.setattr(deps, "_user_store", lambda: users)
    monkeypatch.setattr(ea, "get_email_store", lambda: accounts)
    monkeypatch.setattr(ea, "get_activity", lambda: activity)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    monkeypatch.setattr(ea.settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(ea.settings, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(ea.settings, "PUBLIC_BASE_URL", "https://app.example.com")
    token = create_access_token(user.id, False, username=user.username)
    return TestClient(app), users, accounts, user, other, token


def test_tenant_oauth_state_is_signed_expiring_and_nonce_bound():
    state = google.make_tenant_state("u", "t", "nonce-1")
    assert google.verify_tenant_state(state)[:3] == ("u", "t", "nonce-1")
    assert google.verify_tenant_state(state.replace("nonce-1", "nonce-2")) is None
    expired = google.make_tenant_state("u", "t", "nonce-1", now=0)
    assert google.verify_tenant_state(expired) is None
    assert google.verify_tenant_state(google.make_state("u")) is None


def test_tenant_oauth_authorize_requires_live_membership(tmp_path, monkeypatch):
    client, users, accounts, user, other, token = _tenant_api(tmp_path, monkeypatch)
    path = "/api/v1/email-accounts/google/authorize"
    assert client.get(path, params={"token": token}).status_code == 400
    assert client.get(path, params={"token": token, "tenant_id": "absent"}).status_code == 403
    response = client.get(path, params={"token": token, "tenant_id": other}, follow_redirects=False)
    assert response.status_code == 302
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    assert google.verify_tenant_state(state)[:2] == (user.id, other)
    users.revoke_membership(other, user.id)
    assert client.get(path, params={"token": token, "tenant_id": other}).status_code == 403


def test_signed_but_unreserved_tenant_oauth_state_is_rejected(tmp_path, monkeypatch):
    client, users, accounts, user, other, token = _tenant_api(tmp_path, monkeypatch)
    state = google.make_tenant_state(user.id, other, "unreserved")
    response = client.get(
        "/api/v1/email-accounts/google/callback",
        params={"code": "a", "state": state}, follow_redirects=False,
    )
    assert "gmail=error:expired-state" in response.headers["location"]


def test_tenant_oauth_callback_rechecks_membership_and_rejects_replay(tmp_path, monkeypatch):
    client, users, accounts, user, other, token = _tenant_api(tmp_path, monkeypatch)
    path = "/api/v1/email-accounts/google/authorize"
    callback = "/api/v1/email-accounts/google/callback"
    monkeypatch.setattr(google, "exchange_code", lambda code: {
        "access_token": f"AT-{code}", "refresh_token": "RT",
        "expires_in": 3600, "id_token": "x.eyJlbWFpbCI6Im1pbmVAZ21haWwuY29tIn0.",
    })
    authorize = client.get(path, params={"token": token, "tenant_id": other}, follow_redirects=False)
    state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    users.revoke_membership(other, user.id)
    denied = client.get(callback, params={"code": "a", "state": state}, follow_redirects=False)
    assert "gmail=error:tenant-access" in denied.headers["location"]
    assert accounts.list_for_user(user.id, tenant_id=other) == []

    users.grant_membership(other, user.id, "member")
    authorize = client.get(path, params={"token": token, "tenant_id": other}, follow_redirects=False)
    state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    connected = client.get(callback, params={"code": "b", "state": state}, follow_redirects=False)
    assert "gmail=connected:" in connected.headers["location"]
    assert [a["email"] for a in accounts.list_for_user(user.id, tenant_id=other)] == ["mine@gmail.com"]
    assert accounts.list_for_user(user.id, tenant_id="the-best-estimators-llc") == []
    replay = client.get(callback, params={"code": "c", "state": state}, follow_redirects=False)
    assert "gmail=error:expired-state" in replay.headers["location"]
    account_id = accounts.list_for_user(user.id, tenant_id=other)[0]["id"]
    assert accounts.get_credentials(account_id, user.id, tenant_id=other)["access_token"] == "AT-b"


def test_tenant_account_routes_cannot_read_delete_or_send_from_other_workspace(tmp_path, monkeypatch):
    client, users, accounts, user, other, token = _tenant_api(tmp_path, monkeypatch)
    first = "the-best-estimators-llc"
    accounts.connect(user.id, "first@gmail.com", access_token="A", tenant_id=first)
    account_id = accounts.list_for_user(user.id, tenant_id=first)[0]["id"]
    url = "/api/v1/email-accounts"
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-ID": other}
    assert client.get(url, headers={"Authorization": f"Bearer {token}"}).status_code == 400
    assert client.get(url, headers=headers).json() == []
    assert client.delete(f"{url}/{account_id}", headers=headers).status_code == 404
    assert client.post(f"{url}/{account_id}/send-test", headers=headers).status_code == 404
    assert accounts.get_credentials(account_id, user.id, tenant_id=first) is not None
    users.revoke_membership(other, user.id)
    assert client.get(url, headers=headers).status_code == 403


def test_tenant_callback_revoked_during_exchange_does_not_store_tokens(tmp_path, monkeypatch):
    client, users, accounts, user, other, token = _tenant_api(tmp_path, monkeypatch)
    authorize = client.get(
        "/api/v1/email-accounts/google/authorize",
        params={"token": token, "tenant_id": other}, follow_redirects=False,
    )
    state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]

    def exchange(_code):
        users.revoke_membership(other, user.id)
        return {
            "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
            "id_token": "x.eyJlbWFpbCI6Im1pbmVAZ21haWwuY29tIn0.",
        }

    monkeypatch.setattr(google, "exchange_code", exchange)
    response = client.get(
        "/api/v1/email-accounts/google/callback",
        params={"code": "a", "state": state}, follow_redirects=False,
    )
    assert "gmail=error:tenant-access" in response.headers["location"]
    assert accounts.list_for_user(user.id, tenant_id=other) == []


def test_gmail_inbox_and_export_refuse_other_tenant_account(tmp_path, monkeypatch):
    client, users, accounts, user, other, token = _tenant_api(tmp_path, monkeypatch)
    monkeypatch.setattr(gi, "get_email_store", lambda: accounts)
    accounts.connect(
        user.id, "first@gmail.com", access_token="A", refresh_token="R",
        tenant_id="the-best-estimators-llc", scopes=google.OAUTH_SCOPES,
    )
    account_id = accounts.list_for_user(user.id, tenant_id="the-best-estimators-llc")[0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-ID": other}
    assert client.get("/api/v1/gmail/messages", params={"account_id": account_id}, headers=headers).status_code == 404
    assert client.get("/api/v1/gmail/export/addresses.xlsx", params={"account_id": account_id}, headers=headers).status_code == 404
    assert client.post("/api/v1/gmail/send", json={
        "account_id": account_id, "to": "person@example.com", "subject": "Hi",
    }, headers=headers).status_code == 404
    users.revoke_membership(other, user.id)
    assert client.get("/api/v1/gmail/messages", params={"account_id": account_id}, headers=headers).status_code == 403
