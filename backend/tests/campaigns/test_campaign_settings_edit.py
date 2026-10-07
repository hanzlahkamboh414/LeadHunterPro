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


def test_running_campaign_can_replace_deleted_sender(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=2)
    old_id, new_id = ctx["account_ids"]
    campaign = _make_campaign(ctx, emails=["a@x.com"], account_ids=[old_id])
    ctx["store"].set_status(campaign["id"], status="running")
    ctx["store"].set_draft(campaign["id"], "a@x.com", 0, "Saved subject", "Saved body")
    assert ctx["email_store"].delete(old_id, ctx["user"].id)

    response = ctx["client"].put(f"/api/v1/campaigns/{campaign['id']}", json={
        "name": campaign["name"], "subject": campaign["subject"],
        "body": campaign["body"], "account_id": new_id,
    })
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"
    assert response.json()["account_ids"] == [new_id]
    assert ctx["store"].get_draft(campaign["id"], "a@x.com", 0) == {
        "subject": "Saved subject", "body": "Saved body",
    }

    sent = []
    monkeypatch.setattr(google, "send_gmail", lambda *args, **kwargs:
                        sent.append(kwargs["from_email"]) or {"id": "m1"})
    assert ctx["sched"].run_once()["sent"] == 1
    assert sent == ["sender2@gmail.com"]


def test_paused_campaign_can_replace_deleted_sender_without_resuming(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=2)
    old_id, new_id = ctx["account_ids"]
    campaign = _make_campaign(ctx, emails=["a@x.com"], account_ids=[old_id])
    ctx["store"].set_status(campaign["id"], status="paused", paused_reason="user")
    assert ctx["email_store"].delete(old_id, ctx["user"].id)

    response = ctx["client"].put(f"/api/v1/campaigns/{campaign['id']}", json={
        "name": campaign["name"], "subject": campaign["subject"],
        "body": campaign["body"], "account_id": new_id,
    })
    assert response.status_code == 200, response.text
    assert response.json()["account_ids"] == [new_id]
    assert response.json()["status"] == "paused"
    assert ctx["sched"].run_once()["sent"] == 0


def test_sender_change_during_draft_prevents_send_from_old_account(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=2)
    old_id, new_id = ctx["account_ids"]
    campaign = _make_campaign(ctx, emails=["a@x.com"], account_ids=[old_id])
    ctx["store"].set_status(campaign["id"], status="running")
    sent = []
    monkeypatch.setattr(google, "send_gmail", lambda *args, **kwargs:
                        sent.append(kwargs["from_email"]) or {"id": "m1"})
    real_access_token = ctx["sched"]._access_token

    def switch_while_preparing(**kwargs):
        assert ctx["store"].update_campaign(
            campaign["id"], ctx["user"].id,
            name=campaign["name"], subject=campaign["subject"],
            body=campaign["body"], account_id=new_id, account_ids=[],
        )
        return "old-token"

    monkeypatch.setattr(ctx["sched"], "_access_token", switch_while_preparing)
    assert ctx["sched"].run_once()["sent"] == 0
    assert sent == []
    monkeypatch.setattr(ctx["sched"], "_access_token", real_access_token)
    assert ctx["sched"].run_once()["sent"] == 1
    assert sent == ["sender2@gmail.com"]


def test_referral_followup_is_rejected_before_scheduling(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    ctx["leads"].save(_dossier("a@x.com"))
    response = ctx["client"].post("/api/v1/campaigns", json={
        "name": "Test", "account_id": ctx["account_ids"][0],
        "subject": "Estimating support", "body": "Could I share a work sample?",
        "emails": ["a@x.com"], "start_at": _iso(NOW),
        "followups": [{"after_days": 3, "subject": "Checking in",
                       "body": "Could you point me to the person who handles estimating?"}],
    })
    assert response.status_code == 422
    assert "Follow-up 1" in response.json()["detail"]


def test_corrected_followup_requeues_content_blocked_send(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    campaign = _make_campaign(ctx, emails=["a@x.com"], followups=[
        {"after_days": 3, "subject": "Checking in",
         "body": "Could you point me to the person who handles estimating?"},
    ])
    store = ctx["store"]
    first = store.sends(campaign["id"], ctx["user"].id)[0]
    store.mark_sent(first["id"], subject="Estimating support",
                    sent_at=_iso(NOW - timedelta(days=4)),
                    account_id=ctx["account_ids"][0])
    store.queue_followup(campaign["id"], "a@x.com", step=1,
                         not_before=_iso(NOW - timedelta(days=1)))
    followup = next(row for row in store.sends(campaign["id"], ctx["user"].id)
                    if row["step"] == 1)
    store.mark_failed(followup["id"], error="email asks recipient for another contact")

    response = ctx["client"].put(f"/api/v1/campaigns/{campaign['id']}", json={
        "name": campaign["name"], "subject": campaign["subject"],
        "body": campaign["body"],
        "followups": [{"after_days": 3, "subject": "Checking in",
                       "body": "Would a short sample estimate be useful?"}],
    })
    assert response.status_code == 200, response.text
    retried = next(row for row in response.json()["sends"] if row["step"] == 1)
    assert retried["state"] == "pending"
    assert retried["attempts"] == 0
    assert retried["error"] == ""
