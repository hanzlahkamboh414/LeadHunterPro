"""Open tracking + the sends view + editing a started campaign's pitch.

Tracking: every campaign send embeds a self-hosted 1x1 GIF whose URL names
the send row (HMAC-signed). The pixel endpoint is unauthenticated — the
signature is the auth — and a forged token gets the same gif but marks
nothing. Only a SENT row can open; the first open time is kept forever.

Counting is honest, not naive: an <img> request carries no viewer
identity, so two glitches are filtered by time — a fire within the
post-send grace is the SENDER opening their own Sent copy (not counted),
and a fire within the dedupe window of the last counted open is the same
view re-fetched by the mail client's image proxy (Gmail does this several
times per open — without the dedupe, one view showed as 3x).

Sends view: sends() now carries opened_at / opened_count / replied_at per
lead, so the Campaigns screen can show the whole story — which lead, via
which account, when sent, when the follow-up is due, when opened, when
answered.

Pitch edit: PUT /campaigns/{id} changes name/subject/body on the campaign
row; the scheduler reads the campaign's pitch live, so every send that has
NOT gone out yet uses the new text, while already-sent rows keep the
subject they were actually sent with.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email import message_from_bytes

from fastapi.testclient import TestClient

import app.email_accounts.google as google
from app.campaigns.tracking import (
    OPEN_DEDUPE_S, OPEN_SEND_GRACE_S, PIXEL_GIF, is_repeat_view,
    is_sender_selfcheck, parse_token, pixel_token, pixel_url,
)
from app.main import app

from tests.campaigns.test_multiaccount_ai import (
    _capture_send, _make_campaign, _setup,
)


def _sent_row(ctx, campaign_id: int, email: str) -> dict:
    rows = [s for s in ctx["store"].sends(campaign_id, ctx["user"].id)
            if s["email"] == email and s["state"] == "sent"]
    assert rows, f"no sent row for {email}"
    return rows[-1]


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) -
            timedelta(hours=hours)).isoformat()


def _backdate(ctx, send_id: int, *, sent_at: str | None = None,
              last_open_at: str | None = None) -> None:
    """Rewind a send row's clocks so a pixel fire 'now' is outside the
    grace/dedupe windows (tests can't wait an hour for the real thing)."""
    conn = ctx["store"]._conn()
    if sent_at is not None:
        conn.execute("UPDATE campaign_sends SET sent_at = ? WHERE id = ?",
                     (sent_at, send_id))
    if last_open_at is not None:
        conn.execute("UPDATE campaign_sends SET last_open_at = ? WHERE id = ?",
                     (last_open_at, send_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Token + URL shape
# ---------------------------------------------------------------------------

def test_pixel_token_roundtrip():
    token = pixel_token(42)
    assert parse_token(token) == 42
    # Bad shape, bad signature, non-numeric id — all rejected.
    assert parse_token("not-a-token") is None
    assert parse_token("42:deadbeef") is None
    assert parse_token("") is None


def test_pixel_token_is_per_send_id():
    assert pixel_token(1) != pixel_token(2)


def test_pixel_url_shape():
    url = pixel_url(7)
    assert url.endswith(".png")
    assert "/api/v1/campaigns/track/" in url
    assert parse_token(url.rsplit("/", 1)[1][:-4]) == 7


# ---------------------------------------------------------------------------
# The pixel endpoint
# ---------------------------------------------------------------------------

def test_pixel_marks_open_anonymously(tmp_path, monkeypatch):
    """The <img> tag carries no JWT — the signature is the auth. A real
    token (after the send grace has passed) marks the open: first counted
    time kept, count grows — but only for NEW views, an hour apart."""
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=["a@x.com"])
    ctx["sched"].run_once()
    row = _sent_row(ctx, c["id"], "a@x.com")
    _backdate(ctx, row["id"], sent_at=_ago(2))  # past the send grace

    anon = TestClient(app)  # no Authorization header at all
    r = anon.get(f"/api/v1/campaigns/track/{pixel_token(row['id'])}.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/gif")
    assert r.headers["cache-control"] == "no-store, max-age=0"
    assert r.content == PIXEL_GIF

    sends = ctx["store"].sends(c["id"], ctx["user"].id)
    mine = next(s for s in sends if s["id"] == row["id"])
    assert mine["opened_at"] != ""
    assert mine["opened_count"] == 1
    first_open = mine["opened_at"]

    # Immediate re-fetch: SAME view (the mail client's image proxy asks
    # repeatedly) — count stays 1, the FIRST open time stays.
    r2 = anon.get(f"/api/v1/campaigns/track/{pixel_token(row['id'])}.png")
    assert r2.status_code == 200 and r2.content == PIXEL_GIF
    mine = next(s for s in ctx["store"].sends(
        c["id"], ctx["user"].id) if s["id"] == row["id"])
    assert mine["opened_count"] == 1
    assert mine["opened_at"] == first_open

    # An hour later it IS a new view: count grows, first time still kept.
    _backdate(ctx, row["id"], last_open_at=_ago(2))
    r3 = anon.get(f"/api/v1/campaigns/track/{pixel_token(row['id'])}.png")
    assert r3.status_code == 200
    mine = next(s for s in ctx["store"].sends(
        c["id"], ctx["user"].id) if s["id"] == row["id"])
    assert mine["opened_count"] == 2
    assert mine["opened_at"] == first_open


def test_sender_selfcheck_open_not_counted(tmp_path, monkeypatch):
    """The user report: the SENDER opening their own Sent copy right
    after sending fired the pixel and showed as opens. A fire within the
    post-send grace is that self-check — nothing is marked."""
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=["a@x.com"])
    ctx["sched"].run_once()
    row = _sent_row(ctx, c["id"], "a@x.com")
    # No backdating: sent_at is 'now', the fetch is within the grace.

    anon = TestClient(app)
    r = anon.get(f"/api/v1/campaigns/track/{pixel_token(row['id'])}.png")
    assert r.status_code == 200 and r.content == PIXEL_GIF  # same gif
    mine = next(s for s in ctx["store"].sends(
        c["id"], ctx["user"].id) if s["id"] == row["id"])
    assert mine["opened_at"] == ""
    assert mine["opened_count"] == 0


def test_open_window_helpers():
    """The two time filters, at their boundaries — and failing OPEN when
    a timestamp can't be parsed (a bad value must never hide a real open)."""
    now = datetime.now(timezone.utc).isoformat()

    def _s_ago(seconds: float) -> str:
        return (datetime.now(timezone.utc) -
                timedelta(seconds=seconds)).isoformat()

    # Sender self-check: within the grace yes, past it no.
    assert is_sender_selfcheck(_s_ago(60), now) is True
    assert is_sender_selfcheck(_s_ago(OPEN_SEND_GRACE_S + 60), now) is False
    assert is_sender_selfcheck("", now) is False          # never sent?
    assert is_sender_selfcheck("garbage", now) is False   # fail open
    # Repeat view: within the dedupe window yes, past it / never no.
    assert is_repeat_view(_s_ago(60), now) is True
    assert is_repeat_view(_s_ago(OPEN_DEDUPE_S + 60), now) is False
    assert is_repeat_view("", now) is False               # first open
    assert is_repeat_view("garbage", now) is False        # fail open
    # The windows are the documented constants.
    assert OPEN_SEND_GRACE_S == 300
    assert OPEN_DEDUPE_S == 3600


def test_forged_token_marks_nothing(tmp_path, monkeypatch):
    """Same gif, no DB change — there is no probing oracle."""
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=["a@x.com"])
    ctx["sched"].run_once()
    row = _sent_row(ctx, c["id"], "a@x.com")

    anon = TestClient(app)
    for bad in ("123:deadbeef", "abc:def", "0:x", "123:" + pixel_token(123).split(":")[1]):
        r = anon.get(f"/api/v1/campaigns/track/{bad}.png")
        assert r.status_code == 200
        assert r.content == PIXEL_GIF
    mine = next(s for s in ctx["store"].sends(
        c["id"], ctx["user"].id) if s["id"] == row["id"])
    assert mine["opened_at"] == ""
    assert mine["opened_count"] == 0


def test_pending_row_cannot_be_marked_opened(tmp_path, monkeypatch):
    """A valid token naming a not-yet-sent row marks nothing — only a SENT
    row can open."""
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=["a@x.com", "b@x.com"])
    ctx["sched"].run_once()  # one-send-per-pass: only a@x.com is out
    pending = next(s for s in ctx["store"].sends(
        c["id"], ctx["user"].id) if s["state"] == "pending")

    anon = TestClient(app)
    r = anon.get(f"/api/v1/campaigns/track/{pixel_token(pending['id'])}.png")
    assert r.status_code == 200
    after = next(s for s in ctx["store"].sends(
        c["id"], ctx["user"].id) if s["id"] == pending["id"])
    assert after["opened_at"] == "" and after["opened_count"] == 0


# ---------------------------------------------------------------------------
# The scheduler embeds the pixel; the email carries it
# ---------------------------------------------------------------------------

def test_scheduler_send_carries_tracking_url(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    sent = _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=["a@x.com"])
    ctx["sched"].run_once()

    assert len(sent) == 1
    url = sent[0]["tracking_url"]
    assert "/api/v1/campaigns/track/" in url and url.endswith(".png")
    # The token names the send row that just went out.
    row = _sent_row(ctx, c["id"], "a@x.com")
    assert parse_token(url.rsplit("/", 1)[1][:-4]) == row["id"]


def test_send_gmail_multipart_with_tracking(monkeypatch):
    """With a tracking URL the message is multipart/alternative: the plain
    body PLUS an HTML part (escaped, line-broken) ending in the pixel."""
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "m1"}

    def fake_post(url, *, headers, json, timeout):
        captured["raw"] = json["raw"]
        return FakeResp()

    monkeypatch.setattr(google.requests, "post", fake_post)
    google.send_gmail(
        "AT", to="lead@acme.com", subject="S",
        body="Hi Jane,\n\nSaw <Acme> won a bid.\nCheers",
        from_email="me@gmail.com",
        tracking_url="https://x.example/api/v1/campaigns/track/5:abc.png")

    raw = base64.urlsafe_b64decode(captured["raw"] + "=" *
                                   (-len(captured["raw"]) % 4))
    msg = message_from_bytes(raw)
    assert msg.is_multipart()
    parts = msg.get_payload()
    assert parts[0].get_content_type() == "text/plain"
    assert "Saw <Acme> won a bid" in parts[0].get_payload(decode=True).decode()
    html_part = parts[1]
    assert html_part.get_content_type() == "text/html"
    # get_payload(decode=True) unfolds the quoted-printable encoding the
    # email library applies, so the raw source is compared as real text.
    html = html_part.get_payload(decode=True).decode()
    # The body is HTML-escaped (no injected tags) and the pixel is last.
    assert "&lt;Acme&gt;" in html
    assert 'src="https://x.example/api/v1/campaigns/track/5:abc.png"' in html
    assert "<br>" in html


def test_send_gmail_plain_without_tracking(monkeypatch):
    """No tracking URL -> the plain single-part message, as before (test
    sends pass no URL: they create nothing, so there is nothing to track)."""
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "m1"}

    def fake_post(url, *, headers, json, timeout):
        captured["raw"] = json["raw"]
        return FakeResp()

    monkeypatch.setattr(google.requests, "post", fake_post)
    google.send_gmail("AT", to="lead@acme.com", subject="S", body="plain",
                      from_email="me@gmail.com")
    raw = base64.urlsafe_b64decode(captured["raw"] + "=" *
                                   (-len(captured["raw"]) % 4))
    msg = message_from_bytes(raw)
    assert not msg.is_multipart()
    assert msg.get_content_type() == "text/plain"
    assert "plain" in msg.get_payload(decode=True).decode()


# ---------------------------------------------------------------------------
# The sends view payload
# ---------------------------------------------------------------------------

def test_sends_view_fields(tmp_path, monkeypatch):
    """opened_at / opened_count / replied_at come back per lead."""
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=["a@x.com", "b@x.com"],
                       followups=[{"after_days": 3, "subject": "Re: bump",
                                   "body": "bumping"}])
    assert c is not None
    ctx["sched"].run_once()  # a@x.com sent
    row = _sent_row(ctx, c["id"], "a@x.com")

    r = ctx["client"].get(f"/api/v1/campaigns/{c['id']}")
    assert r.status_code == 200
    sends = {s["email"]: s for s in r.json()["sends"]}
    assert sends["a@x.com"]["opened_at"] == ""
    assert sends["a@x.com"]["opened_count"] == 0
    assert sends["a@x.com"]["replied_at"] == ""
    assert sends["a@x.com"]["account_id"] == ctx["account_ids"][0]
    # b@x.com still queued; step-0 rows are due immediately ('' not_before —
    # follow-up rows get a real not_before only once promoted).
    assert sends["b@x.com"]["state"] == "pending"
    assert sends["b@x.com"]["not_before"] == ""


# ---------------------------------------------------------------------------
# Editing a started campaign's pitch
# ---------------------------------------------------------------------------

def test_put_edits_pitch_pending_sends_use_new_text(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    sent = _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=["a@x.com", "b@x.com"])
    ctx["sched"].run_once()  # a@x.com out with the ORIGINAL pitch
    assert "Estimating for" in sent[0]["subject"]

    r = ctx["client"].put(f"/api/v1/campaigns/{c['id']}", json={
        "name": "Q4 GC outreach", "subject": "New {{company_name}} pitch",
        "body": "Hi {{first_name}}, the NEW script.",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Q4 GC outreach"
    assert body["subject"] == "New {{company_name}} pitch"
    assert body["body"] == "Hi {{first_name}}, the NEW script."

    ctx["sched"].run_once()  # b@x.com out with the EDITED pitch
    assert len(sent) == 2
    assert "New Acme Corp pitch" in sent[1]["subject"]
    assert "the NEW script" in sent[1]["body"]

    sends = {s["email"]: s for s in ctx["store"].sends(c["id"], ctx["user"].id)}
    # The sent rows keep the subject they were ACTUALLY sent with.
    assert "Estimating for" in sends["a@x.com"]["subject"]
    assert "New Acme Corp pitch" in sends["b@x.com"]["subject"]


def test_put_404_foreign_campaign(tmp_path, monkeypatch):
    from app.auth.jwt import create_access_token
    from app.auth.models import UserStore

    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    c = _make_campaign(ctx, emails=["a@x.com"])

    # A different user owns nothing here.
    other_store = UserStore(db_path=str(tmp_path / "users.db"))
    other = other_store.create("other", "o@e.com", "pw")
    token = create_access_token(other.id, other.is_admin, username=other.username)
    other_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    r = other_client.put(f"/api/v1/campaigns/{c['id']}", json={
        "name": "x", "subject": "y", "body": "z"})
    assert r.status_code == 404


def test_put_validates_payload(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    c = _make_campaign(ctx, emails=["a@x.com"])
    r = ctx["client"].put(f"/api/v1/campaigns/{c['id']}", json={
        "name": "", "subject": "s", "body": "b"})
    assert r.status_code == 422


def test_edit_does_not_touch_followup_ladder(tmp_path, monkeypatch):
    """The ladder is not editable (its rungs may already be queued per
    lead) — PUT changes nothing about it."""
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    _capture_send(monkeypatch)
    c = _make_campaign(ctx, emails=["a@x.com"],
                       followups=[{"after_days": 3, "subject": "Re: bump",
                                   "body": "bumping"}])
    ctx["client"].put(f"/api/v1/campaigns/{c['id']}", json={
        "name": "n2", "subject": "s2", "body": "b2"})
    fus = ctx["store"].followups(c["id"])
    assert len(fus) == 1
    assert fus[0]["subject"] == "Re: bump"
