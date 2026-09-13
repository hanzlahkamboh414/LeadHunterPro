"""Campaign test-send — the spam check.

POST /campaigns/test-send sends the DRAFT pitch to the user's own address,
rendered with the sample lead, creating nothing (no campaign, no send row,
no CRM event, no already-emailed mark). Covers: the happy path (rendered
sample values + delivered + nothing created), auth/ownership/status guards,
invalid email 422, Gmail refusal -> 502 + honest 'revoked', and token
refresh through the shared ``ensure_access_token`` path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.campaigns.scheduler import _iso
from app.email_accounts import google
from app.main import app

from tests.campaigns.test_campaigns import _setup


def _post(ctx, **over):
    payload = {
        "account_id": ctx["account_id"],
        "to_email": "me@myself.com",
        "subject": "Estimating for {{company_name}}",
        "body": "Hi {{first_name}}, saw {{company_name}} in {{location}}.",
    }
    payload.update(over)
    return ctx["client"].post("/api/v1/campaigns/test-send", json=payload)


def _capture_send(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        google, "send_gmail",
        lambda access_token, **kw: sent.update(token=access_token, **kw)
        or {"id": "m1"},
    )
    return sent


def _fresh_token(ctx):
    """Re-date the connected account's token expiry to the REAL now.

    ``_setup`` connects it with ``NOW + 1h`` where NOW is a FIXED datetime —
    but the test-send endpoint checks expiry against the real wall clock, so
    after that fixed moment passes, every happy-path test-send 409s as an
    expired token (a time-bomb, same family as the frozen-Clock selfcheck
    one). The scheduler-based tests are immune (their Clock is consistent);
    only this endpoint compares fake-NOW to real now.
    """
    ctx["email_store"].update_tokens(
        ctx["account_id"], ctx["user"].id, access_token="AT-1",
        token_expires_at=_iso(datetime.now(timezone.utc) + timedelta(hours=1)),
    )


# ---------------------------------------------------------------------------
# The happy path — rendered draft, delivered, NOTHING created
# ---------------------------------------------------------------------------

def test_test_send_renders_sample_lead_and_sends(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    _fresh_token(ctx)
    sent = _capture_send(monkeypatch)
    resp = _post(ctx)
    assert resp.status_code == 200
    out = resp.json()
    assert out["sent"] is True
    assert out["to"] == "me@myself.com"
    assert out["from_email"] == "sender@gmail.com"
    # The template rendered with the SAMPLE lead, not literal placeholders.
    assert out["subject"] == "Estimating for Acme Construction"
    assert sent["to"] == "me@myself.com"
    assert sent["subject"] == "Estimating for Acme Construction"
    assert sent["body"].startswith("Hi Alex, saw Acme Construction in Dallas, TX.")
    assert sent["from_email"] == "sender@gmail.com"
    assert sent["token"] == "AT-1"


def test_test_send_creates_nothing(tmp_path, monkeypatch):
    """The spam check must never pollute real campaign data: no campaign,
    no send row, and the test recipient is not 'already emailed'."""
    ctx = _setup(tmp_path, monkeypatch)
    _fresh_token(ctx)
    _capture_send(monkeypatch)
    assert _post(ctx).status_code == 200
    assert ctx["store"].list_for_user(ctx["user"].id) == []
    assert ctx["store"].already_sent_emails(
        ctx["user"].id, ["me@myself.com"]) == set()
    # And a REAL campaign to that same address is not blocked afterwards.
    campaign = ctx["store"].create(
        ctx["user"].id, account_id=ctx["account_id"], name="real",
        subject="s", body="b", emails=["me@myself.com"],
        start_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
    )
    assert campaign["pending"] == 1


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_test_send_requires_auth(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    anon = TestClient(app)
    assert anon.post("/api/v1/campaigns/test-send", json={}).status_code == 401


def test_test_send_unknown_account_404(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    resp = _post(ctx, account_id=999)
    assert resp.status_code == 404


def test_test_send_not_your_account_404(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    # An account belonging to someone else is indistinguishable from a
    # missing one — no existence leak.
    ctx["email_store"].connect("someone-else", "theirs@gmail.com",
                               access_token="t")
    ids = [a["id"] for a in ctx["email_store"].list_for_user("someone-else")]
    resp = _post(ctx, account_id=ids[0])
    assert resp.status_code == 404


def test_test_send_revoked_account_409(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    _capture_send(monkeypatch)
    ctx["email_store"].mark_status(ctx["account_id"], ctx["user"].id, "revoked")
    resp = _post(ctx)
    assert resp.status_code == 409
    assert "reconnect" in resp.json()["detail"]


def test_test_send_invalid_email_422(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    _capture_send(monkeypatch)
    assert _post(ctx, to_email="not-an-email").status_code == 422


def test_test_send_empty_subject_422(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    _capture_send(monkeypatch)
    assert _post(ctx, subject="").status_code == 422


# ---------------------------------------------------------------------------
# Failure honesty + token refresh
# ---------------------------------------------------------------------------

def test_test_send_failure_is_502_and_marks_revoked(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    _fresh_token(ctx)

    def refuse(*a, **kw):
        raise RuntimeError("401 invalid_grant")

    monkeypatch.setattr(google, "send_gmail", refuse)
    resp = _post(ctx)
    assert resp.status_code == 502
    assert "reconnect" in resp.json()["detail"].lower()
    assert ctx["email_store"].list_for_user(ctx["user"].id)[0]["status"] == "revoked"


def test_test_send_refreshes_expired_token(tmp_path, monkeypatch):
    """An expired access token is refreshed through the SAME shared path the
    scheduler uses — the test send must never fire a stale token."""
    ctx = _setup(tmp_path, monkeypatch)
    ctx["email_store"].update_tokens(
        ctx["account_id"], ctx["user"].id, access_token="AT-STALE",
        token_expires_at=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
    )
    monkeypatch.setattr(google, "refresh_access_token",
                        lambda rt: {"access_token": "AT-FRESH", "expires_in": 3600})
    sent = _capture_send(monkeypatch)
    assert _post(ctx).status_code == 200
    assert sent["token"] == "AT-FRESH"
    # And the refreshed token is persisted for later sends.
    creds = ctx["email_store"].get_credentials(ctx["account_id"], ctx["user"].id)
    assert creds["access_token"] == "AT-FRESH"


def test_test_send_refresh_failure_is_409(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    ctx["email_store"].update_tokens(
        ctx["account_id"], ctx["user"].id, access_token="AT-STALE",
        token_expires_at=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
    )

    def boom(rt):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(google, "refresh_access_token", boom)
    resp = _post(ctx)
    assert resp.status_code == 409
    assert "reconnect" in resp.json()["detail"]
