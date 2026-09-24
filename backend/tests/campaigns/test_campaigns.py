"""Phase E3 — campaigns + the safety scheduler.

Covers: template rendering (dossier facts only, unknown/empty tokens render
as "" — never a literal placeholder), the CampaignStore (dedup, counts,
promote/resume transitions, per-ACCOUNT daily cap query, already-sent
exclusion, delete cascade), the scheduler with an INJECTED clock and RNG and
mocked Gmail calls (pacing gap, daily cap, 429 -> pause + self-resume,
401 -> account revoked + auto-resume on reconnect, token refresh, lead
missing, completion), and the HTTP contract (create validation, excluded
count, list/detail/pause/resume/delete, cross-user 404).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests
from fastapi.testclient import TestClient

import app.api.v1.campaigns as campaigns_api
from app.auth.activity import ActivityStore
import app.auth.activity as activity_module
from app.campaigns.scheduler import CampaignScheduler, _iso
from app.campaigns.store import CampaignStore
from app.campaigns.templates import context_for, render
from app.email_accounts import google
from app.email_accounts.store import EmailAccountStore
from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.lead_research.service import LeadResearchStore
from app.main import app

#: The suite's stable "now" for this run. It must NOT be a frozen literal
#: date: the API endpoints (test-send, resume, the open pixel) read the REAL
#: clock, so a hardcoded past date makes fixture tokens look expired, past
#: start_at look already-started and post-send grace windows never match —
#: a time-bomb that detonates the day after it's written. Everything below
#: uses NOW relatively (NOW ± timedelta), never as a literal.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _dossier(email: str, company: str = "Acme Corp",
             person: str = "Jane Smith") -> LeadDossier:
    return LeadDossier(
        email=email, domain="acme.com",
        company=CompanyProfile(name=company, industry="gc",
                               location="Dallas TX"),
        person=PersonFindings(name=person, role="Owner", bound=True,
                              role_relevance=True),
        potential_score=8.0, recommendation="contact_now",
    )


class Clock:
    """Mutable fake clock — tests advance it instead of sleeping."""

    def __init__(self, start: datetime = NOW):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _http_error(code: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = code
    return requests.HTTPError(f"{code} error", response=resp)


def _setup(tmp_path, monkeypatch, *, clock: Clock | None = None):
    """All four stores on tmp DBs + a logged-in client + a scheduler with an
    injectable clock (rng pinned to max delay = deterministic pacing)."""
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
                        token_expires_at=_iso(NOW + timedelta(hours=1)))
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
    )
    return {"client": client, "user": user, "email_store": email_store,
            "account_id": account_id, "store": campaign_store,
            "leads": lead_store, "sched": sched}


def _make_campaign(ctx, *, emails=("jane@acme.com", "bob@build.com"),
                   start_at=None, daily_limit=30):
    """A campaign already RUNNING (start in the past) with two saved leads."""
    for e in emails:
        ctx["leads"].save(_dossier(e))
    return ctx["store"].create(
        ctx["user"].id, account_id=ctx["account_id"],
        name="Q3 GC outreach", subject="Estimating for {{company_name}}",
        body="Hi {{first_name}},\n\nSaw {{company_name}} in {{location}}.\n\n— Us",
        emails=list(emails),
        start_at=start_at or _iso(NOW - timedelta(minutes=1)),
        daily_limit=daily_limit,
    )


# ---------------------------------------------------------------------------
# Templates — dossier facts only
# ---------------------------------------------------------------------------

def test_template_context_and_render():
    d = _dossier("jane@acme.com")
    ctx = context_for(d)
    assert ctx["first_name"] == "Jane"
    assert ctx["last_name"] == "Smith"
    assert ctx["company_name"] == "Acme Corp"
    assert ctx["location"] == "Dallas TX"
    out = render("Hi {{first_name}} of {{company_name}}!", ctx)
    assert out == "Hi Jane of Acme Corp!"


def test_template_unknown_and_empty_render_blank():
    """No literal placeholders in a sent email, and no invented facts."""
    out = render("Hi {{first_name}}, {{unknown_token}}!", {"first_name": ""})
    assert out == "Hi , !"


# ---------------------------------------------------------------------------
# Store — persistence + scheduler-support queries
# ---------------------------------------------------------------------------

def test_store_create_dedups_and_counts(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c = store.create("u1", account_id=1, name="n", subject="s", body="b",
                     emails=["a@x.com", "a@x.com", "b@x.com"],
                     start_at=_iso(NOW))
    assert c["pending"] == 2 and c["sent"] == 0
    assert [s["email"] for s in store.sends(c["id"], "u1")] == \
        ["a@x.com", "b@x.com"]


def test_store_ownership(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c = store.create("u1", account_id=1, name="n", subject="s", body="b",
                     emails=["a@x.com"], start_at=_iso(NOW))
    assert store.get(c["id"], "u2") is None
    assert store.sends(c["id"], "u2") is None
    assert store.delete(c["id"], "u2") is False
    assert store.delete(c["id"], "u1") is True
    assert store.get(c["id"], "u1") is None
    assert store.sends(c["id"], "u1") is None or store.sends(c["id"], "u1") == []


def test_store_promote_and_resumes(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    past = store.create("u1", account_id=1, name="past", subject="s", body="b",
                        emails=["a@x.com"], start_at=_iso(NOW - timedelta(hours=1)))
    future = store.create("u1", account_id=1, name="fut", subject="s", body="b",
                          emails=["a@x.com"], start_at=_iso(NOW + timedelta(hours=1)))
    assert store.promote_scheduled(_iso(NOW)) == [past["id"]]
    assert store.get(past["id"], "u1")["status"] == "running"
    assert store.get(future["id"], "u1")["status"] == "scheduled"

    # 429 pause with a resume_at that has not passed yet.
    store.set_status(past["id"], status="paused", paused_reason="rate_limited",
                     resume_at=_iso(NOW + timedelta(hours=1)))
    assert store.resume_rate_limited(_iso(NOW)) == []
    assert store.resume_rate_limited(_iso(NOW + timedelta(hours=2))) == [past["id"]]

    # Account pause + healthy-account resume.
    store.set_status(past["id"], status="paused", paused_reason="account")
    assert store.account_paused_accounts() == [(1, "u1")]
    assert store.resume_for_accounts([1]) == 1
    assert store.get(past["id"], "u1")["status"] == "running"


def test_store_sent_today_is_per_account(tmp_path):
    """Two campaigns on ONE account share the daily cap."""
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c1 = store.create("u1", account_id=7, name="a", subject="s", body="b",
                      emails=["a@x.com", "b@x.com"], start_at=_iso(NOW))
    c2 = store.create("u1", account_id=7, name="b", subject="s", body="b",
                      emails=["c@x.com"], start_at=_iso(NOW))
    c3 = store.create("u1", account_id=9, name="c", subject="s", body="b",
                      emails=["d@x.com"], start_at=_iso(NOW))
    for cid, _email in [(c1["id"], "a@x.com"), (c1["id"], "b@x.com"),
                        (c2["id"], "c@x.com")]:
        s = store.next_pending(cid)
        store.mark_sent(s["id"], subject="s", sent_at=_iso(NOW))
    assert store.sent_today_for_account(7, _iso(NOW)[:10]) == 3
    assert store.sent_today_for_account(9, _iso(NOW)[:10]) == 0


def test_store_already_sent_exclusion(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c1 = store.create("u1", account_id=1, name="a", subject="s", body="b",
                      emails=["a@x.com", "b@x.com"], start_at=_iso(NOW))
    s = store.next_pending(c1["id"])
    store.mark_sent(s["id"], subject="s", sent_at=_iso(NOW))
    assert store.already_sent_emails("u1", ["a@x.com", "b@x.com"]) == {"a@x.com"}
    # Another user's sends never block this user.
    assert store.already_sent_emails("u2", ["a@x.com"]) == set()


def test_store_completed_when_drained(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c = store.create("u1", account_id=1, name="a", subject="s", body="b",
                     emails=["a@x.com"], start_at=_iso(NOW))
    store.promote_scheduled(_iso(NOW))
    s = store.next_pending(c["id"])
    store.mark_sent(s["id"], subject="s", sent_at=_iso(NOW))
    assert store.mark_completed_if_drained(c["id"]) is True
    assert store.get(c["id"], "u1")["status"] == "completed"


# ---------------------------------------------------------------------------
# Scheduler — the safety design, with a fake clock + mocked Gmail
# ---------------------------------------------------------------------------

def test_scheduler_sends_one_per_gap_and_updates_crm(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    c = _make_campaign(ctx)
    sent_calls = []
    monkeypatch.setattr(google, "send_gmail",
                        lambda tok, **kw: sent_calls.append((tok, kw)) or {})

    # Pass 1: campaign is scheduled -> promoted -> first send.
    stats = ctx["sched"].run_once()
    assert stats == {"promoted": 1, "resumed_rate_limited": 0,
                     "resumed_account": 0, "sent": 1, "paused": 0,
                     "replied": 0, "skipped": 0, "bounced": 0}
    assert [e for _, kw in sent_calls for e in [kw["to"]]] == ["jane@acme.com"]
    assert "{{" not in sent_calls[0][1]["body"]
    assert sent_calls[0][1]["subject"] == "Estimating for Acme Corp"

    # Pass 2 immediately: pacing gap (rng = max = 7 min) not elapsed.
    assert ctx["sched"].run_once()["sent"] == 0

    # Pass 3 after 8 minutes: the second lead goes out.
    # (the fake clock advances the whole world, including "today")
    ctx["sched"]._clock.advance(8 * 60)
    assert ctx["sched"].run_once()["sent"] == 1
    assert len(sent_calls) == 2

    # CRM: both leads moved to 'contacted' with an honest timeline event.
    crm = ctx["leads"].get_crm("jane@acme.com")
    assert crm["crm_status"] == "contacted"
    assert any("Q3 GC outreach" in e["detail"] for e in crm["events"])


def test_scheduler_daily_cap_blocks(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})
    # The clock advances a full day — the token expiry must not turn this
    # into a refresh test.
    monkeypatch.setattr(google, "refresh_access_token",
                        lambda rt: {"access_token": "AT-2", "expires_in": 3600})
    c = _make_campaign(ctx, daily_limit=1)
    ctx["sched"].run_once()                      # send 1 of 1 for today
    ctx["sched"]._clock.advance(8 * 60)          # past the pacing gap
    assert ctx["sched"].run_once()["sent"] == 0  # cap says no
    # Tomorrow the cap resets.
    ctx["sched"]._clock.advance(24 * 3600)
    assert ctx["sched"].run_once()["sent"] == 1


def test_scheduler_429_pauses_then_self_resumes(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    c = _make_campaign(ctx)

    def refuse(tok, **kw):
        raise _http_error(429)

    monkeypatch.setattr(google, "send_gmail", refuse)
    stats = ctx["sched"].run_once()
    assert stats["paused"] == 1
    c = ctx["store"].get(c["id"], ctx["user"].id)
    assert c["status"] == "paused"
    assert c["paused_reason"] == "rate_limited"
    assert c["resume_at"] > _iso(NOW)

    # Not yet: cooldown active.
    ctx["sched"]._clock.advance(60)
    assert ctx["sched"].run_once()["sent"] == 0
    # An hour later the campaign self-resumes (no Gmail retry in the same
    # second — the cooldown release and the send are separate passes).
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})
    ctx["sched"]._clock.advance(3600)
    stats = ctx["sched"].run_once()
    assert stats["resumed_rate_limited"] == 1


def test_scheduler_401_revokes_account_and_resumes_on_reconnect(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    c = _make_campaign(ctx)

    def refuse(tok, **kw):
        raise _http_error(401)

    monkeypatch.setattr(google, "send_gmail", refuse)
    assert ctx["sched"].run_once()["paused"] == 1
    # The ACCOUNT is honestly revoked, not just the campaign paused.
    assert ctx["email_store"].list_for_user(ctx["user"].id)[0]["status"] == "revoked"
    assert ctx["store"].get(c["id"], ctx["user"].id)["paused_reason"] == "account"

    # Reconnect (account healthy again) -> campaign auto-resumes.
    ctx["email_store"].mark_status(ctx["account_id"], ctx["user"].id, "connected")
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})
    stats = ctx["sched"].run_once()
    assert stats["resumed_account"] == 1
    assert stats["sent"] == 1


def test_scheduler_refreshes_expired_token(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    # Token expired an hour ago; the refresh must happen before the send.
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "users.db"))
    conn.execute(
        "UPDATE email_accounts SET token_expires_at = ? WHERE id = ?",
        (_iso(NOW - timedelta(hours=1)), ctx["account_id"]),
    )
    conn.commit()
    conn.close()

    refreshed = []
    monkeypatch.setattr(google, "refresh_access_token",
                        lambda rt: refreshed.append(rt) or
                        {"access_token": "AT-NEW", "expires_in": 3600})
    seen_token = []
    monkeypatch.setattr(google, "send_gmail",
                        lambda tok, **kw: seen_token.append(tok) or {})
    _make_campaign(ctx)
    ctx["sched"].run_once()
    assert refreshed == ["RT-1"]
    assert seen_token == ["AT-NEW"]
    creds = ctx["email_store"].get_credentials(ctx["account_id"], ctx["user"].id)
    assert creds["access_token"] == "AT-NEW"


def test_scheduler_refresh_failure_pauses(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "users.db"))
    conn.execute(
        "UPDATE email_accounts SET token_expires_at = ? WHERE id = ?",
        (_iso(NOW - timedelta(hours=1)), ctx["account_id"]),
    )
    conn.commit()
    conn.close()

    def boom(rt):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(google, "refresh_access_token", boom)
    c = _make_campaign(ctx)
    stats = ctx["sched"].run_once()
    assert stats["paused"] == 1
    assert ctx["store"].get(c["id"], ctx["user"].id)["paused_reason"] == "account"


def test_scheduler_missing_lead_fails_send(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})
    # A campaign lead whose dossier does not exist.
    c = ctx["store"].create(
        ctx["user"].id, account_id=ctx["account_id"], name="ghosts",
        subject="s", body="b", emails=["ghost@nowhere.com"],
        start_at=_iso(NOW - timedelta(minutes=1)),
    )
    ctx["sched"].run_once()
    sends = ctx["store"].sends(c["id"], ctx["user"].id)
    assert sends[0]["state"] == "failed"
    assert "not found" in sends[0]["error"]
    assert ctx["store"].get(c["id"], ctx["user"].id)["status"] == "completed"


def test_scheduler_retry_then_give_up(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    calls = []

    def flaky(tok, **kw):
        calls.append(1)
        raise RuntimeError("temporary network blip")

    monkeypatch.setattr(google, "send_gmail", flaky)
    c = _make_campaign(ctx, emails=("jane@acme.com",))
    # Two failed passes -> attempts bumped, still pending.
    ctx["sched"].run_once()
    ctx["sched"]._clock.advance(8 * 60)
    ctx["sched"].run_once()
    s = ctx["store"].sends(c["id"], ctx["user"].id)[0]
    assert s["state"] == "pending" and s["attempts"] == 2
    # Third failure -> failed for good, campaign completes.
    ctx["sched"]._clock.advance(8 * 60)
    ctx["sched"].run_once()
    s = ctx["store"].sends(c["id"], ctx["user"].id)[0]
    assert s["state"] == "failed" and s["attempts"] == 3


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------

def _create_body(ctx, **over):
    body = {
        "name": "Q3 GC outreach",
        "account_id": ctx["account_id"],
        "subject": "Estimating for {{company_name}}",
        "body": "Hi {{first_name}}",
        "emails": ["jane@acme.com", "bob@build.com"],
        "start_at": _iso(NOW + timedelta(hours=1)),
    }
    body.update(over)
    return body


def test_api_create_list_detail(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    ctx["leads"].save(_dossier("jane@acme.com"))
    resp = ctx["client"].post("/api/v1/campaigns", json=_create_body(ctx))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["campaign"]["status"] == "scheduled"
    assert out["campaign"]["pending"] == 2
    assert out["campaign"]["account_email"] == "sender@gmail.com"
    assert out["excluded"] == 0

    listed = ctx["client"].get("/api/v1/campaigns").json()["campaigns"]
    assert len(listed) == 1
    detail = ctx["client"].get(f"/api/v1/campaigns/{out['campaign']['id']}").json()
    assert [s["email"] for s in detail["sends"]] == ["jane@acme.com", "bob@build.com"]


def test_api_create_validations(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    r = ctx["client"].post("/api/v1/campaigns",
                           json=_create_body(ctx, account_id=999))
    assert r.status_code == 404
    r = ctx["client"].post("/api/v1/campaigns",
                           json=_create_body(ctx, start_at="not-a-date"))
    assert r.status_code == 422
    r = ctx["client"].post("/api/v1/campaigns",
                           json=_create_body(ctx, delay_min_s=500, delay_max_s=100))
    assert r.status_code == 422
    # Revoked account -> 409 with the honest reason.
    ctx["email_store"].mark_status(ctx["account_id"], ctx["user"].id, "revoked")
    r = ctx["client"].post("/api/v1/campaigns", json=_create_body(ctx))
    assert r.status_code == 409 and "reconnect" in r.json()["detail"]


def test_api_already_sent_exclusion(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(google, "send_gmail", lambda tok, **kw: {})
    _make_campaign(ctx, emails=("jane@acme.com",))
    ctx["sched"].run_once()  # jane gets emailed by campaign #1

    ctx["leads"].save(_dossier("bob@build.com"))
    r = ctx["client"].post("/api/v1/campaigns", json=_create_body(ctx))
    assert r.status_code == 200
    out = r.json()
    assert out["excluded"] == 1
    assert out["campaign"]["pending"] == 1  # bob only

    # All already-sent -> honest 422, nothing created.
    r = ctx["client"].post("/api/v1/campaigns",
                           json=_create_body(ctx, emails=["jane@acme.com"]))
    assert r.status_code == 422


def test_api_pause_resume_delete(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    ctx["leads"].save(_dossier("jane@acme.com"))
    # start_at in the REAL future: resume compares against datetime.now, not
    # the test's fixed NOW (whose +1h moment has passed — time-bomb).
    cid = ctx["client"].post(
        "/api/v1/campaigns",
        json=_create_body(
            ctx, start_at=_iso(datetime.now(timezone.utc) + timedelta(hours=1)))
    ).json()["campaign"]["id"]

    # Scheduled -> pause -> resume returns it to SCHEDULED (start in future).
    assert ctx["client"].post(f"/api/v1/campaigns/{cid}/pause").json()["status"] == "paused"
    assert ctx["client"].post(f"/api/v1/campaigns/{cid}/resume").json()["status"] == "scheduled"

    # A PAST-start campaign paused -> resumed goes straight back to running.
    # (Genuinely past vs the REAL clock — the endpoint compares against
    # datetime.now, not the test's fixed NOW.)
    real_past = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    past = ctx["store"].create(
        ctx["user"].id, account_id=ctx["account_id"], name="past",
        subject="s", body="b", emails=["bob@build.com"],
        start_at=real_past,
    )
    ctx["store"].set_status(past["id"], status="running")
    ctx["store"].set_status(past["id"], status="paused", paused_reason="user")
    assert ctx["client"].post(
        f"/api/v1/campaigns/{past['id']}/resume").json()["status"] == "running"

    # Double pause / resuming a completed campaign -> honest 409.
    assert ctx["client"].post(f"/api/v1/campaigns/{cid}/pause").json()["status"] == "paused"
    ctx["store"].set_status(cid, status="completed")
    assert ctx["client"].post(f"/api/v1/campaigns/{cid}/resume").status_code == 409

    assert ctx["client"].delete(f"/api/v1/campaigns/{cid}").json()["deleted"] is True
    assert ctx["client"].get(f"/api/v1/campaigns/{cid}").status_code == 404


def test_api_cross_user_isolation(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    ctx["leads"].save(_dossier("jane@acme.com"))
    cid = ctx["client"].post("/api/v1/campaigns",
                             json=_create_body(ctx)).json()["campaign"]["id"]

    from app.auth.jwt import create_access_token
    import app.auth.dependencies as deps
    intruder = deps._user_store().create("intruder", "x@x.com", "password")
    token = create_access_token(intruder.id, intruder.is_admin,
                                username=intruder.username)
    other = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    assert other.get("/api/v1/campaigns").json()["campaigns"] == []
    assert other.get(f"/api/v1/campaigns/{cid}").status_code == 404
    assert other.post(f"/api/v1/campaigns/{cid}/pause").status_code == 404
    assert other.delete(f"/api/v1/campaigns/{cid}").status_code == 404


def test_api_requires_auth(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    anon = TestClient(app)
    assert anon.get("/api/v1/campaigns").status_code == 401
