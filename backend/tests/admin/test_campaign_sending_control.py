"""Primary admin's global campaign sending switch."""

from fastapi.testclient import TestClient

import app.api.v1.admin as admin_api
import app.api.v1.campaigns as campaigns_api
import app.auth.dependencies as auth_deps
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from app.campaigns.store import CampaignStore
from app.main import app


def test_primary_admin_controls_global_sending_without_touching_campaigns(tmp_path, monkeypatch):
    users = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(auth_deps, "_user_store", lambda: users)
    campaigns = CampaignStore(db_path=str(tmp_path / "campaigns.db"))
    monkeypatch.setattr(admin_api, "get_campaign_store", lambda: campaigns)
    monkeypatch.setattr(campaigns_api, "get_campaign_store", lambda: campaigns)
    admin = users.ensure_admin()
    member = users.create("member", "member@example.com", "password")
    admin_client = TestClient(app, headers={"Authorization": f"Bearer {create_access_token(admin.id, True, admin.username)}"})
    member_client = TestClient(app, headers={"Authorization": f"Bearer {create_access_token(member.id, False, member.username)}"})

    assert admin_client.get("/api/v1/admin/campaign-sending").json() == {"paused": False}
    assert member_client.post("/api/v1/admin/campaign-sending/pause").status_code == 403
    assert admin_client.post("/api/v1/admin/campaign-sending/pause").json() == {"paused": True}
    assert admin_client.get("/api/v1/campaigns/sending-status").json()["paused"] is True
    assert admin_client.post("/api/v1/admin/campaign-sending/resume").json() == {"paused": False}
    assert admin_client.get("/api/v1/campaigns/sending-status").json()["paused"] is False
