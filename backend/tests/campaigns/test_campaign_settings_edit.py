"""Campaign edits apply to future work while retaining sent history."""

from datetime import timedelta

from app.campaigns.scheduler import _iso
from app.email_accounts import google

from tests.campaigns.test_campaigns import Clock, NOW, _dossier
from tests.campaigns.test_multiaccount_ai import _setup, _make_campaign


def test_running_delay_uses_completed_send_and_minimum_20_seconds(tmp_path, monkeypatch):
    clock = Clock()
    ctx = _setup(tmp_path, monkeypatch, accounts=2, clock=clock)
    c = _make_campaign(ctx, emails=["a@x.com", "b@x.com"])
    sent = []

    def send_gmail(*args, **kwargs):
        sent.append(kwargs["to"])
        if len(sent) == 1:
            clock.advance(5)  # Gmail accepted the first email 5s after starting.
        return {"id": str(len(sent))}

    monkeypatch.setattr(google, "send_gmail", send_gmail)
    assert ctx["sched"].run_once()["sent"] == 1
    sent_at = next(s["sent_at"] for s in ctx["store"].sends(c["id"], ctx["user"].id)
                   if s["state"] == "sent")
    assert sent_at == _iso(clock.now)

    endpoint = f"/api/v1/campaigns/{c['id']}"
    pitch = {"name": "running", "subject": "Subject", "body": "Body"}
    assert ctx["client"].put(endpoint, json={**pitch, "delay_min_s": 19,
                                             "delay_max_s": 19}).status_code == 422
    updated = ctx["client"].put(endpoint, json={**pitch, "delay_min_s": 20,
                                                 "delay_max_s": 20})
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "running"
    clock.advance(19)
    assert ctx["sched"].run_once()["sent"] == 0
    clock.advance(1)
    assert ctx["sched"].run_once()["sent"] == 1
    assert sent == ["a@x.com", "b@x.com"]


def test_edit_all_settings_preserves_sent_and_updates_pending(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=2)
    for email in ("new@x.com", "newer@x.com"):
        ctx["leads"].save(_dossier(email))
    c = _make_campaign(ctx, emails=["a@x.com", "b@x.com"], account_ids=[ctx["account_ids"][0]],
                       followups=[{"after_days": 3, "subject": "old follow-up", "body": "old"}])
    monkeypatch.setattr(google, "send_gmail", lambda *args, **kwargs: {"id": "m1"})
    assert ctx["sched"].run_once()["sent"] == 1
    endpoint = f"/api/v1/campaigns/{c['id']}"
    response = ctx["client"].put(endpoint, json={
        "name": "Updated", "subject": "New subject", "body": "New body",
        "account_id": ctx["account_ids"][1],
        "account_ids": [ctx["account_ids"][0]],
        "emails": ["new@x.com", "newer@x.com", "a@x.com"],
        "start_at": _iso(NOW - timedelta(seconds=1)),
        "daily_limit": 12, "delay_min_s": 20, "delay_max_s": 45,
        "ai_personalize": True,
        "followups": [{"after_days": 5, "subject": "new follow-up", "body": "new"}],
    })
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["account_ids"] == [ctx["account_ids"][1], ctx["account_ids"][0]]
    assert edited["daily_limit"] == 12
    assert edited["delay_min_s"] == 20 and edited["delay_max_s"] == 45
    assert edited["ai_personalize"] is True
    assert edited["followups"][0]["after_days"] == 5
    rows = edited["sends"]
    assert {(s["email"], s["step"], s["state"]) for s in rows if s["step"] == 0} == {
        ("a@x.com", 0, "sent"), ("new@x.com", 0, "pending"),
        ("newer@x.com", 0, "pending"),
    }
    assert next(s for s in rows if s["email"] == "a@x.com" and s["step"] == 0)["subject"].startswith("Estimating")
    queued = next(s for s in rows if s["email"] == "a@x.com" and s["step"] == 1)
    assert queued["not_before"] == _iso(NOW + timedelta(days=5))
