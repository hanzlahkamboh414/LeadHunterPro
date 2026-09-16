"""Phase E7 — the Gmail inbox HTTP contract (hermetic: every Google call
faked; the EmailAccountStore lives on a tmp DB with fresh ISO tokens so
ensure_access_token never hits the network).

Covers: folder list + auto mark-read on open, the scope guard (an account
connected before gmail.modify gets an honest 409), attachment download
headers, compose with reply threading, label modify + unknown-label
rejection, ownership (another user's account is a 404), and the address
export — person-only filtering (facebookmail/noreply/info/DSN excluded,
jane kept), year/date window query shape, and real-xlsx bytes out.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient

import app.api.v1.gmail_inbox as gi
import app.email_accounts.google as google
from app.email_accounts.store import EmailAccountStore
from app.main import app

_SCOPES = google.OAUTH_SCOPES  # everything granted


def _meta(mid: str, *, frm="", to="", cc="", subject="s", snippet="sn",
          labels=()):
    """The NORMALIZED message dict — the shape google.get_message returns
    (these tests fake that function, not the wire response)."""
    return {"id": mid, "thread_id": "t" + mid, "snippet": snippet,
            "labels": list(labels),
            "headers": {"from": frm, "to": to, "cc": cc, "subject": subject,
                        "date": "", "message-id": f"<{mid}@x>",
                        "in-reply-to": "", "references": ""},
            "text": "", "html": "", "attachments": []}


def _setup(tmp_path, monkeypatch, *, scopes=_SCOPES):
    from app.auth.activity import ActivityStore
    from app.auth.jwt import create_access_token
    from app.auth.models import UserStore
    import app.auth.dependencies as deps

    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    user = user_store.create("tester", "t@x.com", "pw")
    other = user_store.create("other", "o@x.com", "pw")

    email_store = EmailAccountStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(gi, "get_email_store", lambda: email_store)

    monkeypatch.setattr(
        "app.auth.activity._activity_store",
        ActivityStore(db_path=str(tmp_path / "users.db")),
    )

    expires = (datetime.now(timezone.utc)
               + timedelta(hours=1)).isoformat()

    def _connect(u, email):
        email_store.connect(
            u.id, email, access_token="tok-" + email,
            refresh_token="", token_expires_at=expires, scopes=scopes,
        )
        return email_store.list_for_user(u.id)[0]["id"]

    acct = _connect(user, "me@gmail.com")
    other_acct = _connect(other, "them@gmail.com")

    token = create_access_token(user.id, user.is_admin, username=user.username)
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    return client, acct, other_acct


# ---------------------------------------------------------------------------
# Folder list + read
# ---------------------------------------------------------------------------

def test_list_messages_one_folder(monkeypatch, tmp_path):
    calls = []

    def fake_list(token, *, label="", q="", page_token="", limit=50):
        calls.append((label, q, page_token))
        return {"ids": ["m1", "m2"], "next_page_token": "pt",
                "total_estimate": 2}

    def fake_get_message(token, mid, *, metadata_only=False):
        assert metadata_only
        return _meta(mid, frm="Jane <jane@acme.com>", to="me@gmail.com",
                     labels=["INBOX", "UNREAD"] if mid == "m1" else ["INBOX"])

    monkeypatch.setattr(gi.google, "list_message_ids", fake_list)
    monkeypatch.setattr(gi.google, "get_message", fake_get_message)

    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/messages?account_id={acct}&folder=inbox")
    assert r.status_code == 200
    body = r.json()
    assert body["next_page_token"] == "pt"
    assert [m["id"] for m in body["messages"]] == ["m1", "m2"]
    assert body["messages"][0]["unread"] is True
    assert body["messages"][1]["unread"] is False
    assert calls == [("INBOX", "", "")]


def test_list_messages_search_passes_through(monkeypatch, tmp_path):
    seen = {}

    def fake_list(token, *, label="", q="", page_token="", limit=50):
        seen["q"] = q
        return {"ids": [], "next_page_token": "", "total_estimate": 0}

    monkeypatch.setattr(gi.google, "list_message_ids", fake_list)
    monkeypatch.setattr(gi.google, "get_message", lambda *a, **k: _meta("m"))
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/messages?account_id={acct}"
                   "&q=from:jane after:2024/01/01")
    assert r.status_code == 200
    assert seen["q"] == "from:jane after:2024/01/01"


def test_read_message_marks_it_read(monkeypatch, tmp_path):
    modified = []

    def fake_get_message(token, mid, *, metadata_only=False):
        return {"id": mid, "thread_id": "t1", "snippet": "s",
                "labels": ["INBOX", "UNREAD"],
                "headers": {"from": "j", "to": "", "cc": "", "subject": "",
                            "date": "", "message-id": "<m@x>",
                            "in-reply-to": "", "references": ""},
                "text": "body", "html": "", "attachments": []}

    def fake_modify(token, mid, *, add_labels=(), remove_labels=()):
        modified.append((mid, add_labels, remove_labels))
        return {}

    monkeypatch.setattr(gi.google, "get_message", fake_get_message)
    monkeypatch.setattr(gi.google, "modify_message", fake_modify)
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/messages/m9?account_id={acct}")
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "body"
    assert body["unread"] is False  # opening it read it
    assert modified == [("m9", (), ("UNREAD",))]


def test_account_without_modify_scope_reads_but_does_not_mark(monkeypatch, tmp_path):
    """An account connected before gmail.modify: reading still works (the
    mark-read is skipped, not fatal) but a label action is an honest 409."""
    no_modify = google.OAUTH_SCOPES.replace(
        "https://www.googleapis.com/auth/gmail.modify", "").strip()

    def fake_get_message(token, mid, *, metadata_only=False):
        return {"id": mid, "thread_id": "t", "snippet": "",
                "labels": ["INBOX", "UNREAD"],
                "headers": {k: "" for k in
                            ("from", "to", "cc", "subject", "date",
                             "message-id", "in-reply-to", "references")},
                "text": "hi", "html": "", "attachments": []}

    monkeypatch.setattr(gi.google, "get_message", fake_get_message)
    called = []
    monkeypatch.setattr(gi.google, "modify_message",
                        lambda *a, **k: called.append(1) or {})
    client, acct, _ = _setup(tmp_path, monkeypatch, scopes=no_modify)
    r = client.get(f"/api/v1/gmail/messages/m1?account_id={acct}")
    assert r.status_code == 200 and r.json()["unread"] is True
    assert called == []  # never attempted without the scope

    r = client.post(f"/api/v1/gmail/messages/m1/modify",
                    json={"account_id": acct, "add_labels": ["STARRED"]})
    assert r.status_code == 409
    assert "reconnect" in r.json()["detail"].lower()


def test_attachment_download_headers(monkeypatch, tmp_path):
    def fake_get_message(token, mid, *, metadata_only=False):
        return {"id": mid, "thread_id": "t", "snippet": "", "labels": [],
                "headers": {k: "" for k in ("from", "to", "subject")},
                "text": "", "html": "",
                "attachments": [{"attachment_id": "att1",
                                 "filename": 'quote "final".pdf',
                                 "mime_type": "application/pdf", "size": 5}]}

    monkeypatch.setattr(gi.google, "get_message", fake_get_message)
    monkeypatch.setattr(gi.google, "get_attachment",
                        lambda *a, **k: {"data": b"BYTES", "size": 5})
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/attachment/m1/att1?account_id={acct}")
    assert r.status_code == 200
    assert r.content == b"BYTES"
    assert r.headers["content-type"].startswith("application/pdf")
    assert 'filename="quote \'final\'.pdf"' in r.headers["content-disposition"]


def test_other_users_account_is_404(tmp_path, monkeypatch):
    client, _, other_acct = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/messages?account_id={other_acct}")
    assert r.status_code == 404


def test_list_messages_skips_unreadable_rows(monkeypatch, tmp_path):
    """The metadata fetches run in a thread pool — one Google failure inside
    a worker must skip that row, never kill the page (observed live on main:
    real inboxes do return the occasional unreadable id)."""
    def fake_list(token, *, label="", q="", page_token="", limit=50):
        return {"ids": ["dead", "m2"], "next_page_token": "",
                "total_estimate": 2}

    def fake_get_message(token, mid, *, metadata_only=False):
        if mid == "dead":
            raise RuntimeError("google 500")
        return _meta(mid, frm="Jane <jane@acme.com>")

    monkeypatch.setattr(gi.google, "list_message_ids", fake_list)
    monkeypatch.setattr(gi.google, "get_message", fake_get_message)
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/messages?account_id={acct}&folder=inbox")
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["messages"]] == ["m2"]


# ---------------------------------------------------------------------------
# Send + modify
# ---------------------------------------------------------------------------

def test_send_carries_reply_headers(monkeypatch, tmp_path):
    sent = {}

    def fake_send(token, **kw):
        sent.update(kw)
        return {"id": "new1", "threadId": "t9"}

    monkeypatch.setattr(gi.google, "send_gmail", fake_send)
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.post("/api/v1/gmail/send", json={
        "account_id": acct, "to": "jane@acme.com", "subject": "Re: Quote",
        "body": "Here", "cc": "bob@x.com", "in_reply_to": "<o@x>",
        "references": "<o@x>",
    })
    assert r.status_code == 200
    assert r.json()["sent"] is True
    assert sent["cc"] == "bob@x.com"
    assert sent["in_reply_to"] == "<o@x>"
    assert sent["from_email"] == "me@gmail.com"


def test_modify_rejects_unknown_labels(monkeypatch, tmp_path):
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.post("/api/v1/gmail/messages/m1/modify",
                    json={"account_id": acct,
                          "add_labels": ["IMPORTANT", "NOT-A-LABEL"]})
    assert r.status_code == 400


def test_modify_star_and_trash(monkeypatch, tmp_path):
    modified = []

    def fake_modify(token, mid, *, add_labels=(), remove_labels=()):
        modified.append((add_labels, remove_labels))
        return {}

    monkeypatch.setattr(gi.google, "modify_message", fake_modify)
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.post("/api/v1/gmail/messages/m1/modify",
                    json={"account_id": acct, "add_labels": ["STARRED"],
                          "remove_labels": ["INBOX"]})
    assert r.status_code == 200
    assert modified == [(("STARRED",), ("INBOX",))]


# ---------------------------------------------------------------------------
# The address export
# ---------------------------------------------------------------------------

def _export_pages(mail):
    """A fake Gmail: one page per call, message metadata per id (batched —
    the export fetches metadata through google.batch_get_message_metadata,
    one call carrying a whole page of ids)."""
    def fake_list(token, *, label="", q="", page_token="", limit=100):
        seen.append(q)
        if page_token:
            return {"ids": [], "next_page_token": "", "total_estimate": 0}
        return {"ids": list(mail), "next_page_token": "MORE",
                "total_estimate": len(mail)}

    seen: list[str] = []

    def fake_batch(token, message_ids):
        return [_meta(mid, **mail[mid]) for mid in message_ids
                if mid in mail]

    return fake_list, fake_batch, seen


def _xlsx_rows(content: bytes) -> list[list[str]]:
    zf = zipfile.ZipFile(io.BytesIO(content))
    root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    out = []
    for row in root.findall(".//m:row", ns):
        out.append([t.text or "" for t in row.findall(".//m:is/m:t", ns)])
    return out


def test_export_sent_addresses_deduped(monkeypatch, tmp_path):
    mail = {
        "s1": {"to": "Jane <jane@acme.com>, Bob <bob@acme.com>",
               "cc": "copy@x.com"},
        "s2": {"to": "jane@acme.com", "cc": ""},
        "s3": {"to": "not-an-email", "cc": ""},
    }
    fake_list, fake_batch, seen = _export_pages(mail)
    monkeypatch.setattr(gi.google, "list_message_ids", fake_list)
    monkeypatch.setattr(gi.google, "batch_get_message_metadata", fake_batch)
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/export/addresses.xlsx"
                   f"?account_id={acct}&source=sent")
    assert r.status_code == 200
    assert seen[0] == "in:sent"
    rows = _xlsx_rows(r.content)
    assert rows[0] == ["email"]
    assert [row[0] for row in rows[1:]] == [
        "bob@acme.com", "copy@x.com", "jane@acme.com"]  # unique + sorted


def test_export_received_persons_only(monkeypatch, tmp_path):
    mail = {
        "i1": {"frm": "Jane <jane@acme.com>"},
        "i2": {"frm": "Facebook <noreply@facebookmail.com>"},
        "i3": {"frm": "Instagram <no-reply@mail.instagram.com>"},
        "i4": {"frm": "Billing <info@shop.com>"},          # role local
        "i5": {"frm": "Mailer Daemon <mailer-daemon@g.com>"},
        "i6": {"frm": "Me <me@gmail.com>"},                # own address
        "i7": {"frm": "Bank <alerts@chase.com>"},          # role local
    }
    fake_list, fake_batch, _ = _export_pages(mail)
    monkeypatch.setattr(gi.google, "list_message_ids", fake_list)
    monkeypatch.setattr(gi.google, "batch_get_message_metadata", fake_batch)
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/export/addresses.xlsx"
                   f"?account_id={acct}&source=received")
    assert r.status_code == 200
    rows = _xlsx_rows(r.content)
    # Only the real person survived the filter.
    assert [row[0] for row in rows[1:]] == ["jane@acme.com"]


def test_export_year_and_date_windows(monkeypatch, tmp_path):
    fake_list, fake_batch, seen = _export_pages({})
    monkeypatch.setattr(gi.google, "list_message_ids", fake_list)
    monkeypatch.setattr(gi.google, "batch_get_message_metadata", fake_batch)
    client, acct, _ = _setup(tmp_path, monkeypatch)

    r = client.get(f"/api/v1/gmail/export/addresses.xlsx"
                   f"?account_id={acct}&source=sent&year=2024")
    assert r.status_code == 200
    assert seen[0] == "in:sent after:2024/01/01 before:2025/01/01"

    r2 = client.get(f"/api/v1/gmail/export/addresses.xlsx"
                    f"?account_id={acct}&source=received"
                    f"&from_date=2024-03-01&to_date=2024-04-01")
    assert r2.status_code == 200
    assert seen[-1] == "in:inbox after:2024/03/01 before:2024/04/01"


def test_export_bad_source_rejected(tmp_path, monkeypatch):
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/export/addresses.xlsx"
                   f"?account_id={acct}&source=everywhere")
    assert r.status_code == 422


def _rate_limit_error():
    """A real requests.HTTPError carrying a 403 response — the shape Gmail's
    userRateLimitExceeded arrives in."""
    import requests as _requests
    resp = _requests.Response()
    resp.status_code = 403
    return _requests.HTTPError("403 Client Error: Forbidden", response=resp)


def _quota_error():
    """The other 403 flavor: Google's own body says the per-minute QUOTA
    ran out (observed live on main 2026-09-15 12:52 — the short burst
    backoff cannot clear it; the minute must roll over)."""
    import requests as _requests
    resp = _requests.Response()
    resp.status_code = 403
    resp._content = (
        b'{"error": {"code": 403, "message": "Quota exceeded for quota '
        b'metric \'Total Query Cost\' and limit \'Units per minute per '
        b'user\' of service \'gmail.googleapis.com\'."}}'
    )
    return _requests.HTTPError("403 Client Error: Forbidden", response=resp)


def test_export_retries_gmail_rate_limit(monkeypatch, tmp_path):
    """A mid-export 403 (observed on main 2026-09-15: the parallel metadata
    burst trips Gmail's per-user limit) must back off and retry — not 500
    the download."""
    steps = [
        {"ids": ["i1"], "next_page_token": "MORE", "total_estimate": 1},
        "RATE",  # page 2 → 403 once
        {"ids": [], "next_page_token": "", "total_estimate": 0},  # retry → end
    ]
    attempts = {"n": 0}

    def fake_list(token, *, label="", q="", page_token="", limit=100):
        step = steps[attempts["n"]]
        attempts["n"] += 1
        if step == "RATE":
            raise _rate_limit_error()
        return step

    monkeypatch.setattr(gi.time, "sleep", lambda s: None)  # no real backoff
    monkeypatch.setattr(gi.google, "list_message_ids", fake_list)
    monkeypatch.setattr(
        gi.google, "batch_get_message_metadata",
        lambda t, ids: [_meta(mid, frm="Jane <jane@acme.com>")
                        for mid in ids])
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/export/addresses.xlsx"
                   f"?account_id={acct}&source=received")
    assert r.status_code == 200
    assert attempts["n"] == 3  # the 403 was retried, not fatal
    rows = _xlsx_rows(r.content)
    assert [row[0] for row in rows[1:]] == ["jane@acme.com"]


def test_export_persistent_rate_limit_is_honest_502(monkeypatch, tmp_path):
    """Gmail that keeps saying 403 after the backoffs ends in an honest 502
    (with the reason logged), never a raw Internal Server Error."""

    def fake_list(token, *, label="", q="", page_token="", limit=100):
        raise _rate_limit_error()

    monkeypatch.setattr(gi.time, "sleep", lambda s: None)
    monkeypatch.setattr(gi.google, "list_message_ids", fake_list)
    monkeypatch.setattr(gi.google, "batch_get_message_metadata",
                        lambda t, ids: [_meta(mid) for mid in ids])
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/export/addresses.xlsx"
                   f"?account_id={acct}&source=received")
    assert r.status_code == 502
    assert "try again" in r.json()["detail"].lower()


def test_quota_exceeded_uses_window_backoff(monkeypatch, tmp_path):
    """A quota 403 waits for the per-minute window (30/60/60s — never the
    short burst delays), and window-length waits are enough: the retry
    after them succeeds and the export completes 200."""
    attempts = {"n": 0}
    sleeps: list[float] = []

    # First two calls raise (quota), third returns the page.
    def fake_list(token, *, label="", q="", page_token="", limit=100):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _quota_error()
        return {"ids": ["i1"], "next_page_token": "", "total_estimate": 1}

    monkeypatch.setattr(gi.time, "sleep", sleeps.append)
    monkeypatch.setattr(gi.google, "list_message_ids", fake_list)
    monkeypatch.setattr(
        gi.google, "batch_get_message_metadata",
        lambda t, ids: [_meta(mid, frm="Jane <jane@acme.com>")
                        for mid in ids])
    client, acct, _ = _setup(tmp_path, monkeypatch)
    r = client.get(f"/api/v1/gmail/export/addresses.xlsx"
                   f"?account_id={acct}&source=received")
    assert r.status_code == 200
    # the waits were WINDOW-length, not burst-length
    assert sleeps == [30.0, 60.0]
    rows = _xlsx_rows(r.content)
    assert [row[0] for row in rows[1:]] == ["jane@acme.com"]


def test_quota_vs_burst_counters_are_separate(monkeypatch):
    """A mixed chain — burst 403s and quota 403s interleaved — never lets
    one budget eat the other: the quota budget is still there after burst
    retries ran their course."""
    order = ["burst", "burst", "burst", "quota", "ok"]
    calls = {"n": 0}

    def fn():
        step = order[calls["n"]]
        calls["n"] += 1
        if step == "burst":
            raise _rate_limit_error()
        if step == "quota":
            raise _quota_error()
        return "done"

    monkeypatch.setattr(gi.time, "sleep", lambda s: None)
    assert gi._rate_retry(fn) == "done"


def test_quota_exceeded_flavor_detection():
    """Only Google's body text separates the two 403s — never the status."""
    assert gi._quota_exceeded(_quota_error()) is True
    assert gi._quota_exceeded(_rate_limit_error()) is False
    assert gi._quota_exceeded(ValueError("no response")) is False


def test_user_rate_limit_message_earns_window_wait():
    """The batch endpoint's own throttle wording ("User-rate limit
    exceeded", seen live 2026-09-15) is a PER-MINUTE window too — without
    this the export burned the 17s burst chain and 502'd a whole year."""
    import requests as _requests
    resp = _requests.Response()
    resp.status_code = 429
    resp._content = (
        b'{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", '
        b'"message": "User-rate limit exceeded.  Retry after '
        b'2026-09-15T20:49:30.000Z", '
        b'"errors": [{"reason": "rateLimitExceeded"}]}}'
    )
    exc = _requests.HTTPError("429 Too Many Requests", response=resp)

    assert gi._quota_exceeded(exc) is True
    # And a bare 429 with no body still retries on the burst chain — the
    # flavor is never guessed from the status code alone.
    bare = _requests.Response()
    bare.status_code = 429
    assert gi._quota_exceeded(
        _requests.HTTPError("429", response=bare)) is False


# ---------------------------------------------------------------------------
# The query builder (unit)
# ---------------------------------------------------------------------------

def test_export_query_shapes():
    assert gi._export_query("sent", None, None, None) == "in:sent"
    assert gi._export_query("received", 2024, None, None) == \
        "in:inbox after:2024/01/01 before:2025/01/01"
    assert gi._export_query("sent", None, "2024-03-01", "2024-04-01") == \
        "in:sent after:2024/03/01 before:2024/04/01"


def test_is_person_filter_unit():
    own = "me@gmail.com"
    assert gi._is_person("jane@acme.com", own)
    assert not gi._is_person("me@gmail.com", own)
    assert not gi._is_person("noreply@facebookmail.com", own)
    assert not gi._is_person("x@mail.instagram.com", own)  # subdomain
    assert not gi._is_person("info@shop.com", own)
    assert not gi._is_person("mailer-daemon@aol.com", own)
    assert not gi._is_person("garbage", own)
    assert not gi._is_person("", own)


# ---------------------------------------------------------------------------
# The admin kill-switch: browse off, export stays on
# ---------------------------------------------------------------------------

def _inbox_mode(monkeypatch, enabled: bool):
    """Point the guard's settings at a temp store with the flag set."""
    import app.auth.settings as auth_settings

    store = auth_settings.AuthSettings(
        db_path=str(tmp_db_path.get()))
    store.set_gmail_inbox_enabled(enabled)
    monkeypatch.setattr(auth_settings, "get_settings", lambda: store)


tmp_db_path: dict = {}


def test_inbox_disabled_gates_browse_but_not_export(monkeypatch, tmp_path):
    """The admin toggle: with the interface OFF, every browsing endpoint
    answers 503 but /gmail/mode and the address EXPORT keep working (the
    export is the production feature)."""
    import app.auth.settings as auth_settings
    tmp_db_path["path"] = str(tmp_path / "settings.db")

    store = auth_settings.AuthSettings(db_path=tmp_db_path["path"])
    store.set_gmail_inbox_enabled(False)
    monkeypatch.setattr(auth_settings, "get_settings", lambda: store)

    fake_list, fake_batch, _ = _export_pages(
        {"i1": {"frm": "Jane <jane@acme.com>"}})
    monkeypatch.setattr(gi.google, "list_message_ids", fake_list)
    monkeypatch.setattr(gi.google, "batch_get_message_metadata", fake_batch)
    monkeypatch.setattr(gi.google, "get_message",
                        lambda *a, **k: _meta("m"))
    client, acct, _ = _setup(tmp_path, monkeypatch)

    # mode probe: reachable, honest state
    r = client.get("/api/v1/gmail/mode")
    assert r.status_code == 200
    assert r.json() == {"inbox_enabled": False, "export_enabled": True}

    # browse endpoints: honest 503
    assert client.get(
        f"/api/v1/gmail/messages?account_id={acct}").status_code == 503
    assert client.get(
        f"/api/v1/gmail/messages/m1?account_id={acct}").status_code == 503
    assert client.post(
        "/api/v1/gmail/send",
        json={"account_id": acct, "to": "a@b.com", "subject": "s",
              "body": "x"}).status_code == 503

    # the export: still 200 with real rows
    r = client.get(f"/api/v1/gmail/export/addresses.xlsx"
                   f"?account_id={acct}&source=received")
    assert r.status_code == 200
    rows = _xlsx_rows(r.content)
    assert [row[0] for row in rows[1:]] == ["jane@acme.com"]


def test_inbox_enabled_default_keeps_everything_working(monkeypatch, tmp_path):
    """Missing row = feature ON (the safe default) — no toggle needed on a
    fresh install."""
    import app.auth.settings as auth_settings
    tmp_db_path["path"] = str(tmp_path / "settings.db")

    store = auth_settings.AuthSettings(db_path=tmp_db_path["path"])
    monkeypatch.setattr(auth_settings, "get_settings", lambda: store)

    monkeypatch.setattr(gi.google, "list_message_ids",
                        lambda *a, **k: {"ids": [], "next_page_token": "",
                                         "total_estimate": 0})
    monkeypatch.setattr(gi.google, "get_message",
                        lambda *a, **k: _meta("m"))
    client, acct, _ = _setup(tmp_path, monkeypatch)

    assert client.get(
        f"/api/v1/gmail/messages?account_id={acct}").status_code == 200
