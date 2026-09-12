"""Phase E4 — follow-ups + reply detection.

Covers: the follow-up ladder (queued only after the previous step SENDS,
gated by not_before = sent_at + after_days, cancelled the moment the lead
replies), reply detection (inbox senders read with metadata only, matched
against sent-but-unreplied leads, throttled per account, best-effort — a
read failure never pauses a campaign), CRM honesty (a reply promotes the
stage forward but never walks 'meeting'/'won' backwards), scope honesty
(accounts connected before E4 — no gmail.readonly — are skipped with a log,
not an error), the E3->E4 campaign_sends migration, and the HTTP contract
(followups in create/detail, validation bounds).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import requests
from fastapi.testclient import TestClient

import app.api.v1.campaigns as campaigns_api
from app.auth.activity import ActivityStore
import app.auth.activity as activity_module
from app.campaigns.scheduler import CampaignScheduler, _iso
from app.campaigns.store import CampaignStore
from app.email_accounts import google
from app.email_accounts.store import EmailAccountStore
from app.lead_research.service import LeadResearchStore
from app.main import app

from tests.campaigns.test_campaigns import (
    NOW,
    Clock,
    _dossier,
    _http_error,
)

READONLY = "https://www.googleapis.com/auth/gmail.readonly"


def _setup(tmp_path, monkeypatch, *, clock: Clock | None = None,
           scopes: str = "", token_ttl: timedelta = timedelta(days=30)):
    """All stores on tmp DBs + a client + a scheduler; the connected Gmail
    carries the given granted scopes ('' = a pre-E4 account)."""
    from app.auth.jwt import create_access_token
    from app.auth.models import UserStore
    import app.auth.dependencies as deps

    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    user = user_store.create("testuser", "test@example.com", "password")

    monkeypatch.setattr(
        activity_module, "_activity_store",
        ActivityStore(db_path=str(tmp_path / "users.db")),
    )

    email_store = EmailAccountStore(db_path=str(tmp_path / "users.db"))
    email_store.connect(user.id, "sender@gmail.com",
                        access_token="AT-1", refresh_token="RT-1",
                        token_expires_at=_iso(NOW + token_ttl),
                        scopes=scopes)
    account_id = email_store.list_for_user(user.id)[0]["id"]

    campaign_store = CampaignStore(db_path=str(tmp_path / "campaigns.db"))
    monkeypatch.setattr(campaigns_api, "get_campaign_store",
                        lambda: campaign_store)
    monkeypatch.setattr(campaigns_api, "get_email_store",
                        lambda: email_store)

    lead_store = LeadResearchStore(db_path=str(tmp_path / "leads.db"))
    _orig_save = lead_store.save
    lead_store.save = lambda d, **kw: _orig_save(
        d, user_id=kw.pop("user_id", "") or user.id)

    token = create_access_token(user.id, user.is_admin, username=user.username)
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})

    sched = CampaignScheduler(
        campaign_store, email_store, lead_store,
        clock=clock or Clock(), rng=lambda a, b: b,  # always the max gap
        reply_interval_s=0,   # every pass checks (tests control time)
    )
    return {"client": client, "user": user, "email_store": email_store,
            "account_id": account_id, "store": campaign_store,
            "leads": lead_store, "sched": sched}


def _make_campaign(ctx, *, emails=("jane@acme.com",), followups=None):
    """A RUNNING campaign with saved leads and (optionally) a follow-up
    ladder: [{after_days, subject, body}, ...]."""
    for e in emails:
        ctx["leads"].save(_dossier(e))
    return ctx["store"].create(
        ctx["user"].id, account_id=ctx["account_id"],
        name="Q3 GC outreach", subject="Estimating for {{company_name}}",
        body="Hi {{first_name}}, saw {{company_name}}.",
        emails=list(emails),
        start_at=_iso(NOW - timedelta(minutes=1)),
        followups=followups,
    )


_FU = [{"after_days": 3, "subject": "Re: {{company_name}} estimating",
        "body": "Hi {{first_name}}, following up."}]


# ---------------------------------------------------------------------------
# Store — the ladder + replies
# ---------------------------------------------------------------------------

def test_store_followups_persisted(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c = store.create("u1", account_id=1, name="n", subject="s", body="b",
                     emails=["a@x.com"], start_at=_iso(NOW), followups=_FU)
    fus = store.followups(c["id"])
    assert fus == [{"step": 1, "after_days": 3,
                    "subject": "Re: {{company_name}} estimating",
                    "body": "Hi {{first_name}}, following up."}]
    assert store.followup_after_days(c["id"], 1) == 3
    assert store.followup_after_days(c["id"], 2) is None


def test_store_next_pending_respects_not_before(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c = store.create("u1", account_id=1, name="n", subject="s", body="b",
                     emails=["a@x.com"], start_at=_iso(NOW))
    # Original sent, follow-up queued for +3 days.
    s = store.next_pending(c["id"])
    store.mark_sent(s["id"], subject="s", sent_at=_iso(NOW))
    store.queue_followup(c["id"], "a@x.com", step=1,
                         not_before=_iso(NOW + timedelta(days=3)))
    # Not due yet — with a now, nothing is next; without one (E3 shape),
    # the row is visible for inspection.
    assert store.next_pending(c["id"], _iso(NOW + timedelta(days=1))) is None
    assert store.next_pending(c["id"])["step"] == 1
    # Day 3: the follow-up becomes the next due row.
    due = store.next_pending(c["id"], _iso(NOW + timedelta(days=3)))
    assert due is not None and due["step"] == 1 and due["email"] == "a@x.com"


def test_store_mark_replied_cancels_pending(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c = store.create("u1", account_id=1, name="n", subject="s", body="b",
                     emails=["a@x.com"], start_at=_iso(NOW), followups=_FU)
    s = store.next_pending(c["id"])
    store.mark_sent(s["id"], subject="s", sent_at=_iso(NOW))
    store.queue_followup(c["id"], "a@x.com", step=1,
                         not_before=_iso(NOW + timedelta(days=3)))
    assert store.mark_replied(c["id"], "a@x.com",
                              received_at=_iso(NOW + timedelta(days=1)),
                              subject="Re: s") is True
    assert store.is_replied(c["id"], "a@x.com")
    rows = {r["step"]: r for r in store.sends(c["id"], "u1")}
    assert rows[1]["state"] == "skipped" and rows[1]["error"] == "lead replied"
    # Re-queueing after a reply is a no-op (the ladder stays stopped).
    assert store.queue_followup(c["id"], "a@x.com", step=1,
                                not_before="") is False
    g = store.get(c["id"], "u1")
    assert g["replied"] == 1 and g["skipped"] == 1


def test_store_unreplied_sent_and_accounts(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c = store.create("u1", account_id=5, name="n", subject="s", body="b",
                     emails=["a@x.com", "b@x.com"], start_at=_iso(NOW))
    for e in ("a@x.com", "b@x.com"):
        s = store.next_pending(c["id"])
        store.mark_sent(s["id"], subject="s", sent_at=_iso(NOW))
    store.mark_replied(c["id"], "a@x.com", received_at=_iso(NOW), subject="")
    assert store.accounts_with_activity() == [(5, "u1")]
    assert [r["email"] for r in store.unreplied_sent(5)] == ["b@x.com"]
    # The reply-check throttle stamp round-trips.
    assert store.get_reply_check(5) == ""
    store.set_reply_check(5, _iso(NOW))
    assert store.get_reply_check(5) == _iso(NOW)


def test_store_migrates_e3_sends_table(tmp_path):
    """An E3-shaped campaigns.db (no step column, UNIQUE(campaign_id,email))
    is rebuilt in place with every row carried forward."""
    db = str(tmp_path / "c.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE campaign_sends ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER NOT NULL, "
        "email TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', "
        "subject TEXT NOT NULL DEFAULT '', sent_at TEXT NOT NULL DEFAULT '', "
        "attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', "
        "UNIQUE (campaign_id, email))"
    )
    conn.execute(
        "INSERT INTO campaign_sends (campaign_id, email, state, subject, "
        "sent_at) VALUES (1, 'a@x.com', 'sent', 's', ?)", (_iso(NOW),))
    conn.commit()
    conn.close()

    store = CampaignStore(db_path=db)  # __init__ runs the migration
    cols = [r[1] for r in store._conn().execute(
        "PRAGMA table_info(campaign_sends)")]
    assert "step" in cols and "not_before" in cols
    rows = store.sends(1, "u1") or []  # no ownership row -> None; read raw
    if rows:
        assert rows[0]["step"] == 0
    else:
        raw = store._conn().execute(
            "SELECT email, state, step FROM campaign_sends").fetchall()
        assert raw == [("a@x.com", "sent", 0)]
    # New rows queue fine on the rebuilt table.
    store.queue_followup(1, "a@x.com", step=1, not_before="")


# ---------------------------------------------------------------------------
# Scheduler — the ladder
# ---------------------------------------------------------------------------

def test_scheduler_followup_sends_after_days(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    c = _make_campaign(ctx, followups=_FU)
    sent_calls = []
    monkeypatch.setattr(google, "send_gmail",
                        lambda tok, **kw: sent_calls.append(kw) or {})

    ctx["sched"].run_once()  # step 0 out
    assert [kw["to"] for kw in sent_calls] == ["jane@acme.com"]

    # The follow-up is queued but gated: not_before = now + 3 days.
    fu_row = next(r for r in ctx["store"].sends(c["id"], ctx["user"].id)
                  if r["step"] == 1)
    assert fu_row["state"] == "pending"
    assert fu_row["not_before"].startswith(
        _iso(NOW + timedelta(days=3))[:10])

    # Day 2: still nothing. Day 3 + pacing gap: the follow-up goes out with
    # ITS OWN rendered subject/body.
    ctx["sched"]._clock.advance(2 * 86400)
    assert ctx["sched"].run_once()["sent"] == 0
    ctx["sched"]._clock.advance(86400 + 8 * 60)
    ctx["sched"].run_once()
    assert len(sent_calls) == 2
    assert sent_calls[1]["subject"] == "Re: Acme Corp estimating"
    assert "following up" in sent_calls[1]["body"]

    # Drained: campaign completed, lead CRM carries the follow-up note.
    assert ctx["store"].get(c["id"], ctx["user"].id)["status"] == "completed"
    crm = ctx["leads"].get_crm("jane@acme.com")
    assert crm["crm_status"] == "contacted"  # follow-ups never re-promote
    assert any("follow-up #1" in e["detail"] for e in crm["events"])


def test_scheduler_followup_not_queued_when_replied(tmp_path, monkeypatch):
    """A reply detected before the ladder rung fires stops it entirely."""
    ctx = _setup(tmp_path, monkeypatch, scopes=READONLY)
    c = _make_campaign(ctx, followups=_FU)
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})

    ctx["sched"].run_once()  # step 0 sent -> would queue step 1
    # The reply arrives (detection path records it + cancels).
    ctx["store"].mark_replied(c["id"], "jane@acme.com",
                              received_at=_iso(NOW + timedelta(hours=2)),
                              subject="we're interested")
    # Even a manual re-queue attempt stays stopped (UNIQUE + IGNORE).
    assert ctx["store"].queue_followup(
        c["id"], "jane@acme.com", step=1, not_before="") is False

    ctx["sched"]._clock.advance(4 * 86400)
    stats = ctx["sched"].run_once()
    assert stats["sent"] == 0
    g = ctx["store"].get(c["id"], ctx["user"].id)
    assert g["status"] == "completed" and g["skipped"] == 1
    # The reply itself only advances CRM when DETECTED (covered below);
    # here the ladder simply stopped.
    assert ctx["leads"].get_crm("jane@acme.com")["crm_status"] == "contacted"


def test_scheduler_followup_never_downgrades_crm(tmp_path, monkeypatch):
    """A lead already at 'meeting' keeps that stage when a follow-up sends
    (the note still lands on the timeline)."""
    ctx = _setup(tmp_path, monkeypatch)
    c = _make_campaign(ctx, followups=_FU)
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})

    ctx["sched"].run_once()  # step 0 -> 'contacted'
    ctx["leads"].set_crm("jane@acme.com", status="meeting",
                         user_id=ctx["user"].id, username="")
    ctx["sched"]._clock.advance(3 * 86400 + 8 * 60)
    ctx["sched"].run_once()  # follow-up sends
    crm = ctx["leads"].get_crm("jane@acme.com")
    assert crm["crm_status"] == "meeting"
    assert any("follow-up #1" in e["detail"] for e in crm["events"])


# ---------------------------------------------------------------------------
# Scheduler — reply detection
# ---------------------------------------------------------------------------

def test_scheduler_detects_reply_and_sets_crm(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, scopes=READONLY)
    c = _make_campaign(ctx, followups=_FU)
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})
    inbox = [{"from": "Promo <spam@shop.com>", "subject": "sale"},
             {"from": "Jane Smith <jane@acme.com>", "subject": "Re: estimating"}]
    calls = []
    monkeypatch.setattr(google, "list_inbox_senders",
                        lambda tok, **kw: calls.append(kw) or inbox)

    ctx["sched"].run_once()  # step 0 sent
    ctx["sched"]._clock.advance(2 * 3600)
    stats = ctx["sched"].run_once()  # reply check fires

    assert stats["replied"] == 1
    assert len(calls) == 1  # only one inbox read
    # The reply is recorded, the pending follow-up is skipped, CRM advanced.
    g = ctx["store"].get(c["id"], ctx["user"].id)
    assert g["replied"] == 1 and g["skipped"] == 1
    assert g["status"] == "completed"
    crm = ctx["leads"].get_crm("jane@acme.com")
    assert crm["crm_status"] == "replied"
    assert any("lead replied" in e["detail"] for e in crm["events"])
    # Unmatched senders (spam) recorded nothing.
    assert not ctx["store"].is_replied(c["id"], "spam@shop.com")


def test_scheduler_reply_detection_throttled(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, scopes=READONLY)
    _make_campaign(ctx)
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})
    calls = []
    monkeypatch.setattr(google, "list_inbox_senders",
                        lambda tok, **kw: calls.append(1) or [])
    ctx["sched"].run_once()
    ctx["sched"].run_once()
    ctx["sched"].run_once()
    # The fixture's interval is 0, so every pass re-read. A normally
    # configured scheduler reads at most once per interval window.
    sched2 = CampaignScheduler(
        ctx["store"], ctx["email_store"], ctx["leads"],
        clock=ctx["sched"]._clock, rng=lambda a, b: b,
        reply_interval_s=600,
    )
    before = len(calls)
    ctx["sched"]._clock.advance(700)  # past the interval since the last stamp
    sched2.run_once()
    sched2.run_once()
    sched2.run_once()
    assert len(calls) == before + 1  # read once, then throttled twice


def test_scheduler_reply_detection_needs_readonly(tmp_path, monkeypatch):
    """A pre-E4 account (no gmail.readonly) is skipped honestly — no read,
    no error, sending untouched."""
    ctx = _setup(tmp_path, monkeypatch, scopes="")  # gmail.send only
    _make_campaign(ctx)
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})
    called = []
    monkeypatch.setattr(google, "list_inbox_senders",
                        lambda tok, **kw: called.append(1) or [])
    ctx["sched"].run_once()
    ctx["sched"]._clock.advance(3600)
    ctx["sched"].run_once()
    assert called == []  # never read without the scope


def test_scheduler_reply_check_failure_is_best_effort(tmp_path, monkeypatch):
    """A failing inbox read never pauses a campaign or revokes the account."""
    ctx = _setup(tmp_path, monkeypatch, scopes=READONLY)
    c = _make_campaign(ctx)
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})
    monkeypatch.setattr(google, "list_inbox_senders",
                        lambda tok, **kw: (_ for _ in ()).throw(_http_error(403)))
    stats = ctx["sched"].run_once()
    assert stats["replied"] == 0 and stats["paused"] == 0
    # The campaign was never paused (it completed by sending, which is the
    # point: the failed read changed nothing about the send path).
    g = ctx["store"].get(c["id"], ctx["user"].id)
    assert g["status"] in ("running", "completed")
    assert g["paused_reason"] == ""
    assert ctx["email_store"].list_for_user(ctx["user"].id)[0]["status"] == "connected"


def test_scheduler_reply_detection_refreshes_token(tmp_path, monkeypatch):
    """An expired access token at check time is refreshed (never at the
    cost of failing the check)."""
    clock = Clock(NOW)
    ctx = _setup(tmp_path, monkeypatch, clock=clock, scopes=READONLY,
                 token_ttl=timedelta(hours=1))
    _make_campaign(ctx)
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})
    seen_tokens = []
    monkeypatch.setattr(google, "refresh_access_token",
                        lambda rt: {"access_token": "AT-2", "expires_in": 3600})
    monkeypatch.setattr(google, "list_inbox_senders",
                        lambda tok, **kw: seen_tokens.append(tok) or [])
    ctx["sched"].run_once()  # step 0 sent while the token was still fresh
    assert seen_tokens == []
    ctx["sched"]._clock.advance(2 * 3600)  # token now expired
    ctx["sched"].run_once()  # reply check refreshes, then reads
    assert seen_tokens == ["AT-2"]  # the refreshed token did the read


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------

def test_api_create_with_followups_and_detail(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    for e in ("jane@acme.com",):
        ctx["leads"].save(_dossier(e))
    body = {
        "name": "Ladder",
        "account_id": ctx["account_id"],
        "subject": "Estimating for {{company_name}}",
        "body": "Hi {{first_name}},",
        "emails": ["jane@acme.com"],
        "start_at": _iso(NOW + timedelta(hours=1)),
        "followups": [
            {"after_days": 3, "subject": "Re: {{company_name}}",
             "body": "Following up, {{first_name}}."},
            {"after_days": 7, "subject": "Last nudge",
             "body": "Closing the file on {{company_name}}."},
        ],
    }
    r = ctx["client"].post("/api/v1/campaigns", json=body)
    assert r.status_code == 200, r.text
    cid = r.json()["campaign"]["id"]

    d = ctx["client"].get(f"/api/v1/campaigns/{cid}").json()
    assert [f["step"] for f in d["followups"]] == [1, 2]
    assert d["followups"][0]["after_days"] == 3
    assert d["sends"][0]["step"] == 0
    assert d["skipped"] == 0 and d["replied"] == 0


def test_api_followup_validation(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    ctx["leads"].save(_dossier("jane@acme.com"))
    base = {
        "name": "X", "account_id": ctx["account_id"],
        "subject": "s", "body": "b", "emails": ["jane@acme.com"],
        "start_at": _iso(NOW + timedelta(hours=1)),
    }
    # Four rungs is past the max ladder depth.
    four = {"after_days": 1, "subject": "s", "body": "b"}
    r = ctx["client"].post("/api/v1/campaigns",
                           json={**base, "followups": [four] * 4})
    assert r.status_code == 422
    # after_days must be a real day count (1..30).
    r = ctx["client"].post("/api/v1/campaigns",
                           json={**base, "followups": [
                               {"after_days": 0, "subject": "s", "body": "b"}]})
    assert r.status_code == 422
    # ...and a follow-up needs its own subject/body (no blank rungs).
    r = ctx["client"].post("/api/v1/campaigns",
                           json={**base, "followups": [
                               {"after_days": 3, "subject": "", "body": "b"}]})
    assert r.status_code == 422


def test_api_account_scopes_roundtrip(tmp_path, monkeypatch):
    """The granted scopes are stored and surfaced (never the tokens)."""
    ctx = _setup(tmp_path, monkeypatch, scopes="openid " + READONLY)
    import app.api.v1.email_accounts as email_accounts_api
    monkeypatch.setattr(email_accounts_api, "get_email_store",
                        lambda: ctx["email_store"])
    accounts = ctx["client"].get("/api/v1/email-accounts").json()
    assert READONLY in accounts[0]["scopes"]
    assert "access_token" not in accounts[0]
