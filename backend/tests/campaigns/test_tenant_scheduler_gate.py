"""Unsafe campaign side effects stay disabled until tenant isolation is complete."""

import pytest
from fastapi.testclient import TestClient

import app.api.v1.campaigns as campaigns_api
from app.campaigns.scheduler import CampaignScheduler
from app.campaigns.tracking import PIXEL_GIF, pixel_token
from app.main import app


def test_tenant_scheduler_rejects_start_and_direct_pass(monkeypatch):
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    scheduler = CampaignScheduler(None, None, None)

    with pytest.raises(RuntimeError, match="tenant isolation"):
        scheduler.start()
    with pytest.raises(RuntimeError, match="tenant isolation"):
        scheduler.run_once()


def test_tenant_tracking_returns_pixel_without_writing(monkeypatch):
    class Store:
        def mark_opened(self, *args, **kwargs):
            pytest.fail("tenant tracking must not write an unscoped send")

    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    monkeypatch.setattr(campaigns_api, "get_campaign_store", lambda: Store())

    response = TestClient(app).get(
        f"/api/v1/campaigns/track/{pixel_token(12)}.png"
    )
    assert response.status_code == 200
    assert response.content == PIXEL_GIF

