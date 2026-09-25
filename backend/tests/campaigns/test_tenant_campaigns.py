"""Campaign owner rows must not be reusable by a second workspace."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import app.api.v1.campaigns as campaigns_api
import app.auth.dependencies as deps
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from app.campaigns.store import CampaignStore
from app.email_accounts.store import EmailAccountStore
from app.main import app
from scripts.prepare_unified_database import build_unified_database


def _copy(tmp_path):
    source = tmp_path / "campaigns.db"
    store = CampaignStore(str(source))
    original = store.create(
        "shared-user", account_id=1, name="First tenant only",
        subject="Hello", body="Hi", emails=["one@example.com"],
        start_at="2026-09-25T00:00:00+00:00",
    )
    target = tmp_path / "unified.db"
    build_unified_database([source], target)
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES ('second', 'Second')")
    return target, original["id"]


def test_offline_copy_assigns_campaigns_without_changing_source(tmp_path):
    target, campaign_id = _copy(tmp_path)
    with sqlite3.connect(target) as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(campaigns)")}
        assert columns["tenant_id"][3] == 1
        assert columns["tenant_id"][4] is None
        assert conn.execute(
            "SELECT tenant_id FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone() == ("the-best-estimators-llc",)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO campaigns (user_id, account_id, name, subject, body, start_at, "
                "created_at, updated_at) VALUES ('u', 1, 'n', 's', 'b', 'now', 'now', 'now')"
            )
    with sqlite3.connect(tmp_path / "campaigns.db") as conn:
        assert "tenant_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(campaigns)")
        }


def test_same_user_cannot_read_or_mutate_other_tenant_campaign(tmp_path, monkeypatch):
    target, campaign_id = _copy(tmp_path)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    store = CampaignStore(str(target))
    first = "the-best-estimators-llc"
    assert store.get(campaign_id, "shared-user", tenant_id=first) is not None
    assert store.get(campaign_id, "shared-user", tenant_id="second") is None
    assert store.list_for_user("shared-user", tenant_id="second") == []
    assert store.sends(campaign_id, "shared-user", tenant_id="second") is None
    assert not store.update_campaign(
        campaign_id, "shared-user", name="wrong", subject="wrong", body="wrong",
        tenant_id="second",
    )
    assert not store.delete(campaign_id, "shared-user", tenant_id="second")
    assert store.get(campaign_id, "shared-user", tenant_id=first)["name"] == "First tenant only"
    with pytest.raises(ValueError, match="tenant_id is required"):
        store.list_for_user("shared-user")


def test_campaign_api_denies_cross_tenant_account_and_campaign(tmp_path, monkeypatch):
    users_source = tmp_path / "users.db"
    campaigns_source = tmp_path / "campaigns.db"
    users = UserStore(str(users_source))
    user = users.create("caller", "caller@example.com", "password")
    emails = EmailAccountStore(str(users_source))
    emails.connect(user.id, "first@gmail.com", access_token="AT")
    account_id = emails.list_for_user(user.id)[0]["id"]
    campaigns = CampaignStore(str(campaigns_source))
    campaign = campaigns.create(
        user.id, account_id=account_id, name="First only", subject="Hello",
        body="Hi", emails=["one@example.com"], start_at="2026-09-25T00:00:00+00:00",
    )
    target = tmp_path / "unified.db"
    build_unified_database([users_source, campaigns_source], target)
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES ('second', 'Second')")
    users = UserStore(str(target))
    users.grant_membership("second", user.id, "member")
    emails = EmailAccountStore(str(target))
    campaigns = CampaignStore(str(target))
    monkeypatch.setattr(deps, "_user_store", lambda: users)
    monkeypatch.setattr(campaigns_api, "get_campaign_store", lambda: campaigns)
    monkeypatch.setattr(campaigns_api, "get_email_store", lambda: emails)
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    token = create_access_token(user.id, False, username=user.username)
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    first = "the-best-estimators-llc"
    assert client.get("/api/v1/campaigns").status_code == 400
    assert client.get("/api/v1/campaigns", headers={"X-Tenant-ID": "second"}).json() == {"campaigns": []}
    assert client.get(f"/api/v1/campaigns/{campaign['id']}", headers={"X-Tenant-ID": "second"}).status_code == 404
    assert client.post(f"/api/v1/campaigns/{campaign['id']}/pause", headers={"X-Tenant-ID": "second"}).status_code == 404
    assert client.delete(f"/api/v1/campaigns/{campaign['id']}", headers={"X-Tenant-ID": "second"}).status_code == 404
    assert client.post("/api/v1/campaigns/test-send", headers={"X-Tenant-ID": "second"}, json={
        "account_id": account_id, "to_email": "self@example.com",
        "subject": "Hi", "body": "Hi",
    }).status_code == 404
    create = client.post("/api/v1/campaigns", headers={"X-Tenant-ID": "second"}, json={
        "name": "Wrong", "account_id": account_id, "subject": "Hi", "body": "Hi",
        "emails": ["two@example.com"], "start_at": "2026-09-26T00:00:00+00:00",
    })
    assert create.status_code == 404
    assert client.get(f"/api/v1/campaigns/{campaign['id']}", headers={"X-Tenant-ID": first}).status_code == 200
    users.revoke_membership("second", user.id)
    assert client.get("/api/v1/campaigns", headers={"X-Tenant-ID": "second"}).status_code == 403
