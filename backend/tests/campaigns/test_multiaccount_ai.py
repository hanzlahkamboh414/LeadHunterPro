"""Phase E5 — multi-account sending + AI personalization.

Multi-account: a campaign carries 1..5 connected accounts; the scheduler
spreads sends across them (least-sent-today, rotating ties), pacing and
the daily cap apply PER ACCOUNT (so volume scales with N accounts), a sick
account steps aside without pausing the campaign, and the campaign only
pauses when NO account is left (429 cooldowns included, auto-resuming).

AI personalization: campaigns with ai_personalize get an AI-written
opening line on the FIRST email, built from the lead's VERIFIED dossier
evidence only, cached per (campaign, lead), best-effort (an AI failure
sends the plain template and never blocks).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests
from fastapi.testclient import TestClient

import app.api.v1.campaigns as campaigns_api
from app.auth.activity import ActivityStore
import app.auth.activity as activity_module
from app.campaigns import personalize
from app.campaigns.scheduler import CampaignScheduler, _iso
from app.campaigns.store import CampaignStore
from app.email_accounts import google
from app.email_accounts.store import EmailAccountStore
from app.lead_research.models import AIEvidence, CompanyProfile, LeadDossier, PersonFindings
from app.lead_research.service import LeadResearchStore
from app.main import app

from tests.campaigns.test_campaigns import NOW, Clock, _dossier, _http_error


def _http_error(code: int) -> requests.HTTPError:  # re-exported shape
    resp = requests.Response()
    resp.status_code = code
    return requests.HTTPError(f"{code} error", response=resp)


def _setup(tmp_path, monkeypatch, *, accounts: int = 2, ai_ask=None,
           clock: Clock | None = None):
    """The test_campaigns._setup shape, generalized to N connected accounts
    + an injectable AI callable."""
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
    account_ids = []
    for i in range(accounts):
        email_store.connect(
            user.id, f"sender{i + 1}@gmail.com",
            access_token=f"AT-{i + 1}", refresh_token=f"RT-{i + 1}",
            token_expires_at=_iso(NOW + timedelta(days=30)))
        account_ids.append(email_store.list_for_user(user.id)[i]["id"])

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
        clock=clock or Clock(), rng=lambda a, b: b, ai_ask=ai_ask,
    )
    return {"client": client, "user": user, "email_store": email_store,
            "account_ids": account_ids, "store": campaign_store,
            "leads": lead_store, "sched": sched}


def _make_campaign(ctx, *, emails, account_ids=None, ai_personalize=False,
                   daily_limit=30):
    """A RUNNING campaign (past start, zero delay -> every pass can send)."""
    for e in emails:
        ctx["leads"].save(_dossier(e))
    return ctx["store"].create(
        ctx["user"].id, account_id=(account_ids or ctx["account_ids"])[0],
        name="E5", subject="Estimating for {{company_name}}",
        body="Hi {{first_name}}, saw {{company_name}}.",
        emails=list(emails),
        start_at=_iso(NOW - timedelta(minutes=1)),
        daily_limit=daily_limit, delay_min_s=0, delay_max_s=0,
        account_ids=(account_ids or ctx["account_ids"])[1:],
        ai_personalize=ai_personalize,
    )


def _capture_send(monkeypatch, fail_for=()):
    """send_gmail capture; raises 429 for the listed from_email addresses."""
    sent = []

    def fake(access_token, **kw):
        if kw.get("from_email") in fail_for:
            raise _http_error(429)
        sent.append(dict(kw, token=access_token))
        return {"id": f"m{len(sent)}"}

    monkeypatch.setattr(google, "send_gmail", fake)
    return sent


# ---------------------------------------------------------------------------
# Store — campaign_accounts, per-account queries, hooks
# ---------------------------------------------------------------------------

def test_store_campaign_accounts_persist(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c = store.create("u1", account_id=3, name="n", subject="s", body="b",
                     emails=["a@x.com"], start_at="2026-01-01T00:00:00+00:00",
                     account_ids=[3, 7, 7, 9])
    # Primary first, duplicates dropped, order preserved.
    assert store.campaign_accounts(c["id"]) == [3, 7, 9]
    assert c["account_ids"] == [3, 7, 9]
    # No extras -> just the primary.
    c2 = store.create("u1", account_id=5, name="n2", subject="s", body="b",
                      emails=["b@x.com"], start_at="2026-01-01T00:00:00+00:00")
    assert store.campaign_accounts(c2["id"]) == [5]


def test_store_backfills_primary_on_reopen(tmp_path):
    """A pre-E5 campaigns.db (no campaign_accounts rows) gets its primary
    accounts backfilled on the next open — E3/E4 campaigns keep working."""
    db = str(tmp_path / "c.db")
    store = CampaignStore(db_path=db)
    c = store.create("u1", account_id=4, name="n", subject="s", body="b",
                     emails=["a@x.com"], start_at="2026-01-01T00:00:00+00:00")
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM campaign_accounts")
    conn.commit()
    conn.close()
    reopened = CampaignStore(db_path=db)
    assert reopened.campaign_accounts(c["id"]) == [4]


def test_store_sent_today_counts_by_actual_account(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c = store.create("u1", account_id=1, name="n", subject="s", body="b",
                     emails=["a@x.com", "b@x.com"], start_at="2026-01-01",
                     account_ids=[1, 2])
    # a sent via account 2 (the extra), b still pending.
    send_a = store.next_pending(c["id"], "2026-01-02T00:00:00+00:00")
    store.mark_sent(send_a["id"], subject="s", sent_at="2026-01-02T10:00:00+00:00",
                    account_id=2)
    today = store.sent_today_for_account(2, "2026-01-02")
    assert today == 1
    assert store.sent_today_for_account(1, "2026-01-02") == 0
    assert store.last_sent_at_for_account(2) == "2026-01-02T10:00:00+00:00"
    # Legacy row (account_id 0) counts via the campaign's primary.
    send_b = store.next_pending(c["id"], "2026-01-02T00:00:00+00:00")
    store.mark_sent(send_b["id"], subject="s", sent_at="2026-01-02T11:00:00+00:00")
    assert store.sent_today_for_account(1, "2026-01-02") == 1
    # And the send queue reports who sent what (raw: 0 = pending/legacy row).
    assert [s["account_id"] for s in store.sends(c["id"], "u1")] == [2, 0]


def test_store_hooks_roundtrip(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c = store.create("u1", account_id=1, name="n", subject="s", body="b",
                     emails=["a@x.com"], start_at="2026-01-01")
    assert store.get_hook(c["id"], "a@x.com") is None  # never generated
    store.set_hook(c["id"], "a@x.com", "Saw the Riverside project.")
    assert store.get_hook(c["id"], "a@x.com") == "Saw the Riverside project."
    # First generation wins — a second set is ignored (no re-ask on retry).
    store.set_hook(c["id"], "a@x.com", "different")
    assert store.get_hook(c["id"], "a@x.com") == "Saw the Riverside project."
    store.set_hook(c["id"], "b@x.com", "")  # generated, honestly empty
    assert store.get_hook(c["id"], "b@x.com") == ""


def test_store_delete_purges_e5_tables(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "c.db"))
    c = store.create("u1", account_id=1, name="n", subject="s", body="b",
                     emails=["a@x.com"], start_at="2026-01-01",
                     account_ids=[1, 2])
    store.set_hook(c["id"], "a@x.com", "hook")
    assert store.delete(c["id"], "u1") is True
    assert store.campaign_accounts(c["id"]) == []
    assert store.get_hook(c["id"], "a@x.com") is None


# ---------------------------------------------------------------------------
# Personalize — verified evidence only
# ---------------------------------------------------------------------------

def _dossier_with_evidence() -> LeadDossier:
    d = _dossier("jane@acme.com")
    d.company.facts = [
        AIEvidence(claim="Acme broke ground on the Riverside mixed-use job",
                   source_url="https://acme.com/news", confidence="verified"),
        AIEvidence(claim="maybe expanding to Austin",
                   source_url="", confidence="unverified"),
    ]
    d.person.evidence = [
        AIEvidence(claim="Jane spoke at the Dallas Builders Summit",
                   source_url="https://acme.com/team",
                   confidence="verified"),
    ]
    return d


def test_personalize_prompt_uses_verified_facts_only():
    prompt = personalize.build_prompt(_dossier_with_evidence())
    assert "Riverside mixed-use job" in prompt
    assert "Dallas Builders Summit" in prompt
    # Unverified material is NEVER shown to the model.
    assert "Austin" not in prompt
    assert "maybe expanding" not in prompt
    assert "verified facts" in prompt.lower() or "VERIFIED" in prompt


def test_personalize_clean_hook_rules():
    assert personalize.clean_hook("  Saw the Riverside job.  ") == \
        "Saw the Riverside job."
    assert personalize.clean_hook('"Quoted opener."') == "Quoted opener."
    assert personalize.clean_hook("NONE") == ""
    assert personalize.clean_hook("") == ""
    assert personalize.clean_hook("x" * 500) == ""       # too long
    assert personalize.clean_hook("Hi {{first_name}}!") == ""  # tokens leak


def test_personalize_clean_hook_never_leaves_dashes():
    """Em/en-dashes are the loudest AI tell — they never survive, even when
    the model uses them despite the prompt."""
    assert personalize.clean_hook("Saw Acme — broke ground") == \
        "Saw Acme, broke ground"
    assert personalize.clean_hook("Saw Acme —broke ground") == \
        "Saw Acme, broke ground"
    assert personalize.clean_hook("a – b") == "a, b"
    assert personalize.clean_hook("x—y") == "x, y"
    # A comma run from the swap collapses to one comma.
    assert personalize.clean_hook("Acme, — Dallas — broke ground") == \
        "Acme, Dallas, broke ground"
    # A dash-only hook sanitizes to nothing honest -> plain send.
    assert personalize.clean_hook("—") == ""


def test_personalize_prompt_bans_dashes_and_greeting():
    prompt = personalize.build_prompt(_dossier_with_evidence())
    assert "—" in prompt           # the rule names the character itself
    assert "no greeting" in prompt.lower()


def test_personalize_greeting_fallbacks():
    assert personalize.greeting_for(_dossier("jane@acme.com")) == "Hi Jane,"
    # No person found -> greet the company.
    d = _dossier("info@acme.com", person="")
    assert personalize.greeting_for(d) == "Hi Acme Corp,"
    # Nobody and nothing to greet -> no greeting line at all.
    d = _dossier("x@y.com", company="", person="")
    assert personalize.greeting_for(d) == ""


def test_personalize_strip_leading_greeting():
    strip = personalize.strip_leading_greeting
    assert strip("Hi Jane, saw Acme.") == "Saw Acme."
    assert strip("Hello Jane Smith,\n\nwe help GCs.") == "We help GCs."
    assert strip("Dear Mr. Smith, quick note") == "Quick note"
    # No greeting -> untouched.
    assert strip("We help GCs estimate.") == "We help GCs estimate."
    # Something that merely starts with 'Hi' but isn't a greeting -> kept.
    assert strip("Hiring is busy, we know.") == "Hiring is busy, we know."


def test_personalize_assemble_opening():
    d = _dossier("jane@acme.com")
    out = personalize.assemble_opening(
        d, "Saw Acme broke ground on the Riverside job.",
        "Hi Jane, we help GCs estimate.")
    assert out == ("Hi Jane,\n\n"
                   "Saw Acme broke ground on the Riverside job.\n\n"
                   "We help GCs estimate.")
    # No hook (NONE / AI down) -> the uniform greeting + script remain.
    assert personalize.assemble_opening(d, "", "Hi Jane, we help GCs.") == \
        "Hi Jane,\n\nWe help GCs."
    # No name at all -> hook + script stack in order, nothing invented.
    d = _dossier("x@y.com", company="", person="")
    assert personalize.assemble_opening(d, "Nice hook.", "Plain script.") == \
        "Nice hook.\n\nPlain script."


def test_personalize_generate_hook():
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return "Saw Acme broke ground on the Riverside job."

    hook = personalize.generate_hook(ask, _dossier_with_evidence())
    assert hook == "Saw Acme broke ground on the Riverside job."
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Scheduler — multi-account behavior
# ---------------------------------------------------------------------------

def test_scheduler_spreads_sends_across_accounts(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    sent = _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=[f"l{i}@x.com" for i in range(4)])
    ctx["sched"].run_once()
    ctx["sched"].run_once()  # zero delay -> a second send this same instant
    assert len(sent) == 2
    used = {s["from_email"] for s in sent}
    # Both fresh accounts were used — the tie rotated.
    assert used == {"sender1@gmail.com", "sender2@gmail.com"}


def test_scheduler_dead_account_steps_aside(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    sent = _capture_send(monkeypatch)
    ctx["email_store"].mark_status(ctx["account_ids"][0], ctx["user"].id,
                                   "revoked")
    c = _make_campaign(ctx, emails=[f"l{i}@x.com" for i in range(2)])
    ctx["sched"].run_once()
    ctx["sched"].run_once()
    # All sends went via the healthy account; the campaign finished healthy
    # (never paused for the dead one).
    assert {s["from_email"] for s in sent} == {"sender2@gmail.com"}
    assert ctx["store"].get(c["id"], ctx["user"].id)["status"] == "completed"


def test_scheduler_all_accounts_dead_pauses(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    _capture_send(monkeypatch)
    for aid in ctx["account_ids"]:
        ctx["email_store"].mark_status(aid, ctx["user"].id, "revoked")
    c = _make_campaign(ctx, emails=["l0@x.com"])
    ctx["sched"].run_once()
    assert ctx["store"].get(c["id"], ctx["user"].id)["status"] == "paused"
    # Reconnect one -> the health loop auto-resumes the campaign.
    ctx["email_store"].mark_status(ctx["account_ids"][1], ctx["user"].id,
                                   "connected")
    ctx["sched"].run_once()
    # Resumed AND the pending lead went out -> campaign completed.
    assert ctx["store"].get(c["id"], ctx["user"].id)["status"] == "completed"


def test_scheduler_429_on_one_account_others_continue(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    sent = _capture_send(monkeypatch, fail_for=("sender1@gmail.com",))
    c = _make_campaign(ctx, emails=[f"l{i}@x.com" for i in range(4)])
    clock = Clock()
    sched = CampaignScheduler(ctx["store"], ctx["email_store"], ctx["leads"],
                              clock=clock, rng=lambda a, b: b)
    stats = sched.run_once()  # sends via one account (tie rotation)
    assert stats["sent"] == 1
    stats = sched.run_once()  # picks the other (least-sent) -> 429, cools down
    assert stats["paused"] == 0  # campaign NOT paused — a healthy account remains
    stats = sched.run_once()  # next pass: the healthy account takes over
    assert stats["sent"] == 1
    assert sent[1]["from_email"] == "sender2@gmail.com"
    assert ctx["store"].get(c["id"], ctx["user"].id)["status"] == "running"


def test_scheduler_429_on_all_accounts_pauses_and_resumes(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    sent = _capture_send(monkeypatch, fail_for=("sender1@gmail.com",))
    c = _make_campaign(ctx, emails=["l0@x.com"])
    clock = Clock()
    sched = CampaignScheduler(ctx["store"], ctx["email_store"], ctx["leads"],
                              clock=clock, rng=lambda a, b: b)
    sched.run_once()
    row = ctx["store"].get(c["id"], ctx["user"].id)
    assert row["status"] == "paused"
    assert row["paused_reason"] == "rate_limited"
    assert row["resume_at"] != ""
    # Cooldown over -> the self-heal resumes it (and the send retries).
    clock.advance(3601)
    _capture_send(monkeypatch)  # no failures anymore
    stats = sched.run_once()
    assert stats["resumed_rate_limited"] == 1
    assert stats["sent"] == 1


def test_scheduler_daily_cap_per_account(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    sent = _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=[f"l{i}@x.com" for i in range(3)],
                       daily_limit=1)
    ctx["sched"].run_once()
    ctx["sched"].run_once()
    assert len(sent) == 2  # one per account
    assert {s["from_email"] for s in sent} == \
        {"sender1@gmail.com", "sender2@gmail.com"}
    # Third lead: both accounts capped -> nothing more today, still running.
    stats = ctx["sched"].run_once()
    assert stats["sent"] == 0
    row = ctx["store"].get(c["id"], ctx["user"].id)
    assert row["status"] == "running" and row["pending"] == 1


# ---------------------------------------------------------------------------
# Scheduler — AI opening lines
# ---------------------------------------------------------------------------

def test_scheduler_ai_hook_prepended_and_cached(tmp_path, monkeypatch):
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return "Saw Acme broke ground on the Riverside job."

    ctx = _setup(tmp_path, monkeypatch, ai_ask=ask)
    sent = _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=["jane@acme.com"], ai_personalize=True)
    ctx["sched"].run_once()
    assert len(sent) == 1
    # Greeting first, then the AI line, then the script (its own "Hi" gone,
    # remainder capitalized) — one professional email, greeted exactly once.
    assert sent[0]["body"] == ("Hi Jane,\n\n"
                               "Saw Acme broke ground on the Riverside job."
                               "\n\nSaw Acme Corp.")
    # Cached — a re-run asks the AI nothing new.
    assert ctx["store"].get_hook(c["id"], "jane@acme.com") == \
        "Saw Acme broke ground on the Riverside job."
    assert len(calls) == 1


def test_scheduler_ai_failure_sends_plain(tmp_path, monkeypatch):
    def ask(prompt):
        raise RuntimeError("router busy")

    ctx = _setup(tmp_path, monkeypatch, ai_ask=ask)
    sent = _capture_send(monkeypatch)
    _make_campaign(ctx, emails=["jane@acme.com"], ai_personalize=True)
    stats = ctx["sched"].run_once()
    # Best-effort: the uniform greeting + plain script went out, campaign
    # healthy, and the failure was NOT cached as an honest empty hook.
    assert stats["sent"] == 1
    assert sent[0]["body"] == "Hi Jane,\n\nSaw Acme Corp."
    assert ctx["store"].get_hook(1, "jane@acme.com") is None


def test_scheduler_ai_none_is_honest_and_cached(tmp_path, monkeypatch):
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return "NONE"

    ctx = _setup(tmp_path, monkeypatch, ai_ask=ask)
    sent = _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=["jane@acme.com"], ai_personalize=True)
    ctx["sched"].run_once()
    # NONE -> no hook, but the uniform greeting + script still go out.
    assert sent[0]["body"] == "Hi Jane,\n\nSaw Acme Corp."
    # '' cached = generated, nothing honest to say — never re-asked.
    assert ctx["store"].get_hook(c["id"], "jane@acme.com") == ""
    assert len(calls) == 1


def test_scheduler_ai_off_sends_plain(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)  # no ai_ask injected
    sent = _capture_send(monkeypatch)
    _make_campaign(ctx, emails=["jane@acme.com"], ai_personalize=False)
    ctx["sched"].run_once()
    # AI off -> the user's template exactly as written, untouched.
    assert sent[0]["body"] == "Hi Jane, saw Acme Corp."


def test_scheduler_followup_not_personalized(tmp_path, monkeypatch):
    def ask(prompt):
        return "Saw Acme broke ground on the Riverside job."

    ctx = _setup(tmp_path, monkeypatch, ai_ask=ask)
    sent = _capture_send(monkeypatch)
    clock = Clock()
    sched = CampaignScheduler(ctx["store"], ctx["email_store"], ctx["leads"],
                              clock=clock, rng=lambda a, b: b, ai_ask=ask)
    c = ctx["store"].create(
        ctx["user"].id, account_id=ctx["account_ids"][0],
        name="fu", subject="Hi {{first_name}}",
        body="Hi {{first_name}}, first.",
        emails=["jane@acme.com"],
        start_at=_iso(NOW - timedelta(minutes=1)),
        delay_min_s=0, delay_max_s=0,
        followups=[{"after_days": 3, "subject": "Re: hi",
                    "body": "Hi {{first_name}}, bumping this."}],
        ai_personalize=True,
    )
    ctx["leads"].save(_dossier("jane@acme.com"))
    sched.run_once()  # step 0 — personalized (greeting + hook + script)
    clock.advance(3 * 86400)
    sched.run_once()  # step 1 — the follow-up is NOT personalized
    assert len(sent) == 2
    assert sent[0]["body"] == ("Hi Jane,\n\n"
                               "Saw Acme broke ground on the Riverside job."
                               "\n\nFirst.")
    assert sent[1]["body"] == "Hi Jane, bumping this."


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------

def _create_body(ctx, **over):
    body = {
        "name": "E5 campaign",
        "account_id": ctx["account_ids"][0],
        "account_ids": ctx["account_ids"][1:],
        "subject": "Estimating for {{company_name}}",
        "body": "Hi {{first_name}}",
        "emails": ["jane@acme.com", "bob@build.com"],
        "start_at": _iso(NOW + timedelta(hours=1)),
        "ai_personalize": True,
    }
    body.update(over)
    return body


def test_api_create_multi_account_and_ai(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    ctx["leads"].save(_dossier("jane@acme.com"))
    ctx["leads"].save(_dossier("bob@build.com"))
    resp = ctx["client"].post("/api/v1/campaigns", json=_create_body(ctx))
    assert resp.status_code == 200, resp.text
    out = resp.json()["campaign"]
    assert out["account_ids"] == ctx["account_ids"]
    assert out["ai_personalize"] is True
    assert sorted(out["account_emails"]) == \
        ["sender1@gmail.com", "sender2@gmail.com"]

    listed = ctx["client"].get("/api/v1/campaigns").json()["campaigns"]
    assert listed[0]["account_ids"] == ctx["account_ids"]
    assert len(listed[0]["account_emails"]) == 2


def test_api_create_validates_extra_accounts(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    ctx["leads"].save(_dossier("jane@acme.com"))
    # A foreign account among the extras is a 404, exactly like the primary.
    r = ctx["client"].post("/api/v1/campaigns",
                           json=_create_body(ctx, account_ids=[999]))
    assert r.status_code == 404
    # More than 5 accounts total -> 422.
    r = ctx["client"].post("/api/v1/campaigns",
                           json=_create_body(ctx, account_ids=[2, 3, 4, 5, 6]))
    assert r.status_code == 422
    # A revoked EXTRA account is refused like a revoked primary.
    ctx["email_store"].mark_status(ctx["account_ids"][1], ctx["user"].id,
                                   "revoked")
    r = ctx["client"].post("/api/v1/campaigns", json=_create_body(ctx))
    assert r.status_code == 409 and "reconnect" in r.json()["detail"]
