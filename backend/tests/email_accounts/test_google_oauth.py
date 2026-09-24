"""Phase E2 — Gmail OAuth connect.

Covers: Fernet token encryption at rest (round-trip, empty, wrong key), the
HMAC-signed OAuth state (round-trip, expiry, tamper), the EmailAccountStore
(upsert on re-connect, list NEVER exposes tokens, ownership on credentials,
disconnect deletes), and the HTTP contract — authorize (503 unconfigured,
401 bad token, 302 to Google), callback (error paths redirect honestly; the
happy path with a MOCKED code exchange stores the account + records activity),
list, disconnect, and send-test (mocked Gmail API; a refusal marks the
account revoked and answers 502). No real Google call ever runs.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

import app.api.v1.email_accounts as ea
import app.auth.activity as activity_module
from app.auth.activity import ActivityStore
from app.email_accounts import google
from app.email_accounts.crypto import decrypt_text, encrypt_text
from app.email_accounts.store import EmailAccountStore
from app.main import app


def _setup(tmp_path, monkeypatch):
    """User + email-account store + activity log on tmp DBs, and a logged-in
    client — the test_crm.py pattern."""
    from app.auth.jwt import create_access_token
    from app.auth.models import UserStore
    import app.auth.dependencies as deps

    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    user = user_store.create("testuser", "test@example.com", "password")

    email_store = EmailAccountStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(ea, "get_email_store", lambda: email_store)

    monkeypatch.setattr(
        activity_module, "_activity_store",
        ActivityStore(db_path=str(tmp_path / "users.db")),
    )

    # Deterministic config: tests never depend on a developer's .env.
    monkeypatch.setattr(ea.settings, "GOOGLE_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(ea.settings, "GOOGLE_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(ea.settings, "PUBLIC_BASE_URL", "https://app.example.com")

    token = create_access_token(user.id, user.is_admin, username=user.username)
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    return client, user, email_store


def _fake_id_token(email: str, name: str = "Test User") -> str:
    """A structurally valid id_token payload (unpadded base64url segments) —
    decode_id_token only reads the claims, it never verifies a signature."""
    import base64
    import json

    def seg(obj) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()
        return raw.rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'email': email, 'name': name})}."


# ---------------------------------------------------------------------------
# Crypto — tokens are never plaintext at rest
# ---------------------------------------------------------------------------

def test_crypto_round_trip():
    token = encrypt_text("super-secret-refresh-token")
    assert token != "super-secret-refresh-token"
    assert decrypt_text(token) == "super-secret-refresh-token"


def test_crypto_empty_stays_empty():
    assert encrypt_text("") == ""
    assert decrypt_text("") == ""


def test_crypto_garbage_returns_empty():
    """A token that no longer decrypts (rotated key / corruption) is an
    empty credential, never a crash."""
    assert decrypt_text("not-a-fernet-token") == ""


# ---------------------------------------------------------------------------
# OAuth state — CSRF + user identity across the Google round-trip
# ---------------------------------------------------------------------------

def test_state_round_trip():
    state = google.make_state("user-123")
    assert google.verify_state(state) == "user-123"


def test_state_expired():
    now = time.time()
    state = google.make_state("user-123", now=now - 3600)
    assert google.verify_state(state) is None


def test_state_tampered():
    state = google.make_state("user-123")
    user_id, expiry, sig = state.split(":")
    forged = f"user-999:{expiry}:{sig}"
    assert google.verify_state(forged) is None
    assert google.verify_state("garbage") is None
    assert google.verify_state("") is None


# ---------------------------------------------------------------------------
# Store — upsert, token privacy, ownership, delete
# ---------------------------------------------------------------------------

def test_store_connect_upserts_not_duplicates(tmp_path):
    store = EmailAccountStore(db_path=str(tmp_path / "ea.db"))
    store.connect("u1", "a@gmail.com", access_token="t1", refresh_token="r1")
    store.connect("u1", "a@gmail.com", access_token="t2", refresh_token="r2")
    listed = store.list_for_user("u1")
    assert len(listed) == 1
    creds = store.get_credentials(listed[0]["id"], "u1")
    assert creds["access_token"] == "t2"  # re-connect refreshes the tokens


def test_store_list_never_exposes_tokens(tmp_path):
    store = EmailAccountStore(db_path=str(tmp_path / "ea.db"))
    store.connect("u1", "a@gmail.com", access_token="SECRET-A", refresh_token="SECRET-R")
    row = store.list_for_user("u1")[0]
    assert set(row) == {"id", "provider", "email", "display_name", "status",
                        "scopes", "created_at"}
    assert "SECRET" not in str(row)
    # And at rest, the DB never holds plaintext tokens.
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "ea.db"))
    raw = conn.execute("SELECT access_token FROM email_accounts").fetchone()[0]
    conn.close()
    assert "SECRET-A" not in raw


def test_store_credentials_ownership(tmp_path):
    store = EmailAccountStore(db_path=str(tmp_path / "ea.db"))
    store.connect("u1", "a@gmail.com", access_token="t1")
    listed = store.list_for_user("u1")
    assert store.get_credentials(listed[0]["id"], "u1") is not None
    assert store.get_credentials(listed[0]["id"], "someone-else") is None


def test_store_delete(tmp_path):
    store = EmailAccountStore(db_path=str(tmp_path / "ea.db"))
    store.connect("u1", "a@gmail.com", access_token="t1")
    account_id = store.list_for_user("u1")[0]["id"]
    assert store.delete(account_id, "someone-else") is False   # not yours
    assert store.delete(account_id, "u1") is True
    assert store.list_for_user("u1") == []


# ---------------------------------------------------------------------------
# Authorize — the hop into Google
# ---------------------------------------------------------------------------

def test_google_status_reflects_config(tmp_path, monkeypatch):
    client, user, _ = _setup(tmp_path, monkeypatch)
    assert client.get("/api/v1/email-accounts/google/status").json() == {"configured": True}
    monkeypatch.setattr(ea.settings, "GOOGLE_CLIENT_ID", "")
    assert client.get("/api/v1/email-accounts/google/status").json() == {"configured": False}


def test_authorize_unconfigured_503(tmp_path, monkeypatch):
    client, user, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(ea.settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(ea.settings, "GOOGLE_CLIENT_SECRET", "")
    resp = client.get("/api/v1/email-accounts/google/authorize",
                      params={"token": "anything"})
    assert resp.status_code == 503
    assert "GOOGLE_CLIENT_ID" in resp.json()["detail"]


def test_authorize_bad_token_401(tmp_path, monkeypatch):
    client, user, _ = _setup(tmp_path, monkeypatch)
    resp = client.get("/api/v1/email-accounts/google/authorize",
                      params={"token": "not-a-jwt"})
    assert resp.status_code == 401


def test_authorize_missing_token_401(tmp_path, monkeypatch):
    client, user, _ = _setup(tmp_path, monkeypatch)
    resp = client.get("/api/v1/email-accounts/google/authorize")
    assert resp.status_code == 401


def test_authorize_302_to_google(tmp_path, monkeypatch):
    client, user, _ = _setup(tmp_path, monkeypatch)
    from app.auth.jwt import create_access_token
    token = create_access_token(user.id, user.is_admin, username=user.username)
    resp = client.get("/api/v1/email-accounts/google/authorize",
                      params={"token": token}, follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "gmail.send" in location
    # The state names the user and verifies at the callback.
    from urllib.parse import unquote
    state = unquote([p for p in location.split("&") if p.startswith("state=")][0].split("=", 1)[1])
    assert google.verify_state(state) == user.id


# ---------------------------------------------------------------------------
# Callback — the hop back from Google (code exchange MOCKED)
# ---------------------------------------------------------------------------

def _mock_exchange(monkeypatch, email="newbie@gmail.com"):
    id_token = _fake_id_token(email)
    monkeypatch.setattr(
        google, "exchange_code",
        lambda code: {"access_token": f"AT-{code}", "refresh_token": "RT-1",
                      "expires_in": 3600, "id_token": id_token},
    )


def test_callback_happy_path(tmp_path, monkeypatch):
    client, user, store = _setup(tmp_path, monkeypatch)
    _mock_exchange(monkeypatch)
    state = google.make_state(user.id)
    resp = client.get("/api/v1/email-accounts/google/callback",
                      params={"code": "abc", "state": state}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://app.example.com/settings?gmail=connected:newbie@gmail.com"
    listed = store.list_for_user(user.id)
    assert [a["email"] for a in listed] == ["newbie@gmail.com"]
    assert listed[0]["status"] == "connected"
    # The connect is audited.
    acts = activity_module._activity_store.list(user_id=user.id)
    assert any(a["action"] == "email_account" and "newbie@gmail.com" in a["detail"]
               for a in acts)


def test_callback_google_error(tmp_path, monkeypatch):
    client, user, _ = _setup(tmp_path, monkeypatch)
    resp = client.get("/api/v1/email-accounts/google/callback",
                      params={"error": "access_denied"}, follow_redirects=False)
    assert resp.status_code == 302
    assert "gmail=error:access_denied" in resp.headers["location"]


def test_callback_bad_state(tmp_path, monkeypatch):
    client, user, _ = _setup(tmp_path, monkeypatch)
    _mock_exchange(monkeypatch)
    resp = client.get("/api/v1/email-accounts/google/callback",
                      params={"code": "abc", "state": "forged:1:2"},
                      follow_redirects=False)
    assert "gmail=error:expired-state" in resp.headers["location"]


def test_callback_missing_code(tmp_path, monkeypatch):
    client, user, _ = _setup(tmp_path, monkeypatch)
    state = google.make_state(user.id)
    resp = client.get("/api/v1/email-accounts/google/callback",
                      params={"state": state}, follow_redirects=False)
    assert "gmail=error:missing-code" in resp.headers["location"]


def test_callback_exchange_failure(tmp_path, monkeypatch):
    client, user, store = _setup(tmp_path, monkeypatch)

    def boom(code):
        raise RuntimeError("google down")

    monkeypatch.setattr(google, "exchange_code", boom)
    state = google.make_state(user.id)
    resp = client.get("/api/v1/email-accounts/google/callback",
                      params={"code": "abc", "state": state}, follow_redirects=False)
    assert "gmail=error:exchange-failed" in resp.headers["location"]
    assert store.list_for_user(user.id) == []


def test_callback_reconnect_refreshes_tokens(tmp_path, monkeypatch):
    """Same Gmail connected twice = ONE account, refreshed tokens (the
    UNIQUE upsert), never a duplicate row."""
    client, user, store = _setup(tmp_path, monkeypatch)
    _mock_exchange(monkeypatch)
    state = google.make_state(user.id)
    client.get("/api/v1/email-accounts/google/callback",
               params={"code": "one", "state": state}, follow_redirects=False)
    client.get("/api/v1/email-accounts/google/callback",
               params={"code": "two", "state": google.make_state(user.id)},
               follow_redirects=False)
    assert len(store.list_for_user(user.id)) == 1
    creds = store.get_credentials(store.list_for_user(user.id)[0]["id"], user.id)
    assert creds["access_token"] == "AT-two"


# ---------------------------------------------------------------------------
# List / disconnect / send-test (all auth-header endpoints)
# ---------------------------------------------------------------------------

def test_list_requires_auth(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    anon = TestClient(app)
    assert anon.get("/api/v1/email-accounts").status_code == 401


def test_list_and_disconnect(tmp_path, monkeypatch):
    client, user, store = _setup(tmp_path, monkeypatch)
    store.connect(user.id, "mine@gmail.com", access_token="t", refresh_token="r")
    account_id = store.list_for_user(user.id)[0]["id"]

    listed = client.get("/api/v1/email-accounts").json()
    assert [a["email"] for a in listed] == ["mine@gmail.com"]
    assert "access_token" not in str(listed)

    assert client.delete(f"/api/v1/email-accounts/{account_id}").status_code == 200
    assert client.get("/api/v1/email-accounts").json() == []


def test_disconnect_not_yours_404(tmp_path, monkeypatch):
    client, user, store = _setup(tmp_path, monkeypatch)
    store.connect("someone-else", "theirs@gmail.com", access_token="t")
    account_id = store.list_for_user("someone-else")[0]["id"]
    assert client.delete(f"/api/v1/email-accounts/{account_id}").status_code == 404


def test_send_test_success(tmp_path, monkeypatch):
    client, user, store = _setup(tmp_path, monkeypatch)
    store.connect(user.id, "mine@gmail.com", access_token="GOOD", refresh_token="r")
    account_id = store.list_for_user(user.id)[0]["id"]

    sent = {}
    monkeypatch.setattr(
        google, "send_gmail",
        lambda access_token, **kw: sent.update(token=access_token, **kw) or {"id": "m1"},
    )
    resp = client.post(f"/api/v1/email-accounts/{account_id}/send-test")
    assert resp.status_code == 200
    assert resp.json() == {"id": account_id, "sent": True, "to": "mine@gmail.com"}
    assert sent["token"] == "GOOD"
    assert sent["to"] == "mine@gmail.com"


def test_send_test_failure_marks_revoked(tmp_path, monkeypatch):
    client, user, store = _setup(tmp_path, monkeypatch)
    store.connect(user.id, "mine@gmail.com", access_token="STALE", refresh_token="r")
    account_id = store.list_for_user(user.id)[0]["id"]

    def refuse(*a, **kw):
        raise RuntimeError("401 invalid_grant")

    monkeypatch.setattr(google, "send_gmail", refuse)
    resp = client.post(f"/api/v1/email-accounts/{account_id}/send-test")
    assert resp.status_code == 502
    assert "reconnect" in resp.json()["detail"].lower()
    # The account is honestly marked — not left looking healthy.
    assert store.list_for_user(user.id)[0]["status"] == "revoked"


def test_send_test_success_clears_revoked_flag(tmp_path, monkeypatch):
    """A healthy send-test resets a stale 'revoked' status — the account IS
    working, the UI must not keep saying "Needs reconnect"."""
    client, user, store = _setup(tmp_path, monkeypatch)
    store.connect(user.id, "mine@gmail.com", access_token="GOOD", refresh_token="r")
    account_id = store.list_for_user(user.id)[0]["id"]
    store.mark_status(account_id, user.id, "revoked")

    monkeypatch.setattr(google, "send_gmail",
                        lambda access_token, **kw: {"id": "m1"})
    resp = client.post(f"/api/v1/email-accounts/{account_id}/send-test")
    assert resp.status_code == 200
    assert store.list_for_user(user.id)[0]["status"] == "connected"


def test_send_test_undecryptable_tokens_409(tmp_path, monkeypatch):
    """Tokens that no longer decrypt (key rotated) = reconnect, not fake send."""
    client, user, store = _setup(tmp_path, monkeypatch)
    store.connect(user.id, "mine@gmail.com", access_token="t", refresh_token="r")
    account_id = store.list_for_user(user.id)[0]["id"]
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "users.db"))
    conn.execute("UPDATE email_accounts SET access_token = 'x', refresh_token = 'y' "
                 "WHERE id = ?", (account_id,))
    conn.commit()
    conn.close()
    resp = client.post(f"/api/v1/email-accounts/{account_id}/send-test")
    assert resp.status_code == 409


def _batch_response(parts, boundary="batch_x"):
    """A Google-style multipart/mixed batch reply: each part is an inner
    HTTP response. Parts are (status, echoed message id, json payload) —
    Google echoes the requested id in each part's Content-ID as
    <response-id:m>, which is how a failed inner answer is mapped back to
    the id that was asked for."""
    import requests
    crlf = "\r\n"
    body = ""
    for status, mid, payload in parts:
        body += ("--" + boundary + crlf
                 + "Content-Type: application/http" + crlf
                 + "Content-ID: <response-id:" + mid + ">" + crlf + crlf
                 + "HTTP/1.1 " + str(status) + " "
                 + ("OK" if status == 200 else "Error") + crlf
                 + "Content-Type: application/json; charset=UTF-8" + crlf
                 + crlf + payload + crlf)
    body += "--" + boundary + "--" + crlf
    resp = requests.Response()
    resp.status_code = 200
    resp._content = body.encode()
    resp.headers["Content-Type"] = "multipart/mixed; boundary=" + boundary
    return resp


def _message_json(mid, frm="jane@acme.com"):
    import json
    return json.dumps({
        "id": mid, "threadId": "t" + mid, "snippet": "s",
        "labelIds": ["INBOX"],
        "payload": {"headers": [{"name": "From", "value": frm}]},
    })


def test_batch_get_message_metadata_parses_inner_responses(monkeypatch):
    """The multipart reply is parsed into the same normalized dicts
    get_message(metadata_only=True) returns — headers, snippet, labels."""
    captured = {}

    def fake_post(url, *, headers, data, timeout):
        captured["url"] = url
        captured["auth"] = headers["Authorization"]
        return _batch_response([(200, "m1", _message_json("m1")),
                                (200, "m2",
                                 _message_json("m2", "bob@acme.com"))])

    monkeypatch.setattr(google.time, "sleep", lambda s: None)
    monkeypatch.setattr(google.requests, "post", fake_post)
    out = google.batch_get_message_metadata("tok", ["m1", "m2"])

    assert captured["url"] == google.GMAIL_BATCH_URL
    assert captured["auth"] == "Bearer tok"
    assert [m["id"] for m in out] == ["m1", "m2"]
    assert out[0]["headers"]["from"] == "jane@acme.com"
    assert out[0]["labels"] == ["INBOX"]
    assert out[0]["text"] == ""  # metadata-only shape


def test_batch_get_message_metadata_skips_dead_inner_ids(monkeypatch):
    """A 404 inner response (message gone) is skipped WITHOUT a retry —
    4xx is permanent, only 5xx earns the retry rounds."""
    posts = []
    monkeypatch.setattr(google.time, "sleep", lambda s: None)

    def fake_post(url, *, headers, data, timeout):
        posts.append(data)
        return _batch_response([(200, "m1", _message_json("m1")),
                                (404, "gone", '{"error": {}}')])

    monkeypatch.setattr(google.requests, "post", fake_post)
    out = google.batch_get_message_metadata("tok", ["m1", "gone"])
    assert [m["id"] for m in out] == ["m1"]
    assert len(posts) == 1  # the 404 never triggered a retry round


def test_batch_get_message_metadata_retries_inner_503s(monkeypatch):
    """Inner 503 'Backend Error' answers are TRANSIENT (Gmail's batch
    backend throws them when batches arrive back-to-back — observed live
    2026-09-15: a whole 100-id page 503'd, the same ids succeeded seconds
    later). The failed ids are re-requested, never silently dropped."""
    posts = []
    sleeps = []

    def fake_post(url, *, headers, data, timeout):
        posts.append(data)
        if len(posts) == 1:  # first round: everything 503s
            return _batch_response([(503, "m1", "{}"),
                                    (503, "m2", "{}")])
        return _batch_response([(200, "m1", _message_json("m1")),
                                (200, "m2", _message_json("m2"))])

    monkeypatch.setattr(google.time, "sleep", sleeps.append)
    monkeypatch.setattr(google.requests, "post", fake_post)
    out = google.batch_get_message_metadata("tok", ["m1", "m2"])

    assert [m["id"] for m in out] == ["m1", "m2"]  # both recovered
    assert len(posts) == 2                          # one retry round
    assert sleeps == [google.BATCH_RETRY_BACKOFF_S[0]]


def _requested_ids(data: str) -> list[str]:
    """The message ids a batch POST body asks for (Content-ID: <id:m>)."""
    import re
    return re.findall(r"Content-ID: <id:([^>]+)>", data)


def test_batch_get_message_metadata_gives_up_after_all_rounds(monkeypatch):
    """Ids STILL 503ing after every retry round are skipped with a warning —
    honest data loss, never silent."""
    posts = []
    monkeypatch.setattr(google.time, "sleep", lambda s: None)

    def fake_post(url, *, headers, data, timeout):
        posts.append(data)
        # answer exactly what was asked: m1 succeeds, m2 always 503s
        return _batch_response(
            [(200, "m1", _message_json("m1")) if mid == "m1"
             else (503, mid, "{}") for mid in _requested_ids(data)])

    monkeypatch.setattr(google.requests, "post", fake_post)
    out = google.batch_get_message_metadata("tok", ["m1", "m2"])

    assert [m["id"] for m in out] == ["m1"]
    assert len(posts) == google.BATCH_RETRY_ATTEMPTS  # every round tried


def test_batch_get_message_metadata_splits_over_max_inner(monkeypatch):
    """More than BATCH_MAX_INNER ids become consecutive batch POSTs —
    Google caps one batch at 100 inner requests, and our own chunk size is
    smaller still (the burst bucket; see the constant's note)."""
    posts = []

    def fake_post(url, *, headers, data, timeout):
        posts.append(data)
        # answer every requested id with its own 200
        return _batch_response(
            [(200, mid, _message_json(mid))
             for mid in _requested_ids(data)])

    monkeypatch.setattr(google.time, "sleep", lambda s: None)
    monkeypatch.setattr(google.requests, "post", fake_post)
    ids = [f"m{i}" for i in range(250)]
    out = google.batch_get_message_metadata("tok", ids)

    chunks = [len(_requested_ids(p)) for p in posts]
    assert len(posts) == -(-250 // google.BATCH_MAX_INNER)  # ceil
    assert chunks[-1] == (250 % google.BATCH_MAX_INNER
                          or google.BATCH_MAX_INNER)
    assert all(c <= google.BATCH_MAX_INNER for c in chunks)
    assert len(out) == 250


def test_batch_get_message_metadata_raises_rate_limited_inner(monkeypatch):
    """An inner 403 that is NOT one of Google's rate-limit reasons (here a
    bare quota message with no machine-readable reason) raises
    ``requests.HTTPError`` CARRYING that status and body — so the caller's
    rate/quota retry logic (which reads resp.text) applies to batched calls
    exactly as to single ones."""
    import pytest
    import requests
    monkeypatch.setattr(google.requests, "post", lambda *a, **k: _batch_response(
        [(200, "m1", _message_json("m1")),
         (403, "m2", '{"error": {"message": "Quota exceeded"}}')]))
    with pytest.raises(requests.HTTPError) as excinfo:
        google.batch_get_message_metadata("tok", ["m1", "m2"])
    assert excinfo.value.response.status_code == 403
    assert "quota exceeded" in excinfo.value.response.text.lower()


def test_batch_get_message_metadata_retries_inner_rate_limit(monkeypatch):
    """An inner 429 ('rateLimitExceeded') is the per-user THROTTLE, not a
    failure of the request: only the throttled ids go back for the next
    round and the wait is Google's own 'Retry after' stamp — observed live
    2026-09-15, where treating it as fatal 502'd a whole year's export."""
    import json as _json
    posts, sleeps = [], []

    def fake_post(url, *, headers, data, timeout):
        posts.append(data)
        asked = _requested_ids(data)
        if len(posts) == 1:
            return _batch_response([
                (200, mid, _message_json(mid)) if mid == "m1" else
                (429, mid, _json.dumps({"error": {
                    "code": 429, "status": "RESOURCE_EXHAUSTED",
                    "message": "User-rate limit exceeded.  Retry after "
                               "2020-01-01T00:00:00.000Z",
                    "errors": [{"reason": "rateLimitExceeded"}]}}))
                for mid in asked])
        return _batch_response([(200, mid, _message_json(mid))
                                for mid in asked])

    monkeypatch.setattr(google.time, "sleep", sleeps.append)
    monkeypatch.setattr(google.requests, "post", fake_post)
    out = google.batch_get_message_metadata("tok", ["m1", "m2"])

    assert [m["id"] for m in out] == ["m1", "m2"]  # both recovered
    assert len(posts) == 2
    assert _requested_ids(posts[1]) == ["m2"]      # only the throttled id
    assert sleeps == [1.0]  # the Retry-after stamp is in the past → floor


def test_batch_get_message_metadata_transient_403_reason_is_retried(monkeypatch):
    """A 403 whose body carries a rate-limit REASON (Google's machine-
    readable field) is the throttle too — retried, never fatal."""
    import json as _json
    posts = []

    def fake_post(url, *, headers, data, timeout):
        posts.append(data)
        asked = _requested_ids(data)
        if len(posts) == 1:
            return _batch_response([
                (403, mid, _json.dumps({"error": {
                    "code": 403, "message": "User-rate limit exceeded",
                    "errors": [{"reason": "userRateLimitExceeded"}]}}))
                for mid in asked])
        return _batch_response([(200, mid, _message_json(mid))
                                for mid in asked])

    monkeypatch.setattr(google.time, "sleep", lambda s: None)
    monkeypatch.setattr(google.requests, "post", fake_post)
    out = google.batch_get_message_metadata("tok", ["m1"])

    assert [m["id"] for m in out] == ["m1"]
    assert len(posts) == 2


def test_batch_get_message_metadata_permanent_403_raises_at_once(monkeypatch):
    """A real permission 403 (insufficientPermissions) is NOT transient —
    it raises on the first answer instead of burning retry rounds."""
    import json as _json
    import pytest
    import requests
    posts = []

    def fake_post(url, *, headers, data, timeout):
        posts.append(data)
        return _batch_response([
            (403, mid, _json.dumps({"error": {
                "code": 403, "message": "Insufficient Permission",
                "errors": [{"reason": "insufficientPermissions"}]}}))
            for mid in _requested_ids(data)])

    monkeypatch.setattr(google.time, "sleep", lambda s: None)
    monkeypatch.setattr(google.requests, "post", fake_post)
    with pytest.raises(requests.HTTPError) as excinfo:
        google.batch_get_message_metadata("tok", ["m1"])
    assert excinfo.value.response.status_code == 403
    assert len(posts) == 1  # no retry rounds


def test_batch_get_message_metadata_throttle_leftover_raises(monkeypatch):
    """Ids STILL throttled after every round RAISE — a throttled page means
    those addresses are unknown, so they are never silently skipped the way
    a lone flaky 5xx id is."""
    import json as _json
    import pytest
    import requests
    posts = []

    def fake_post(url, *, headers, data, timeout):
        posts.append(data)
        return _batch_response([
            (200, "m1", _message_json("m1")) if mid == "m1" else
            (429, mid, _json.dumps({"error": {
                "code": 429, "message": "User-rate limit exceeded",
                "errors": [{"reason": "rateLimitExceeded"}]}}))
            for mid in _requested_ids(data)])

    monkeypatch.setattr(google.time, "sleep", lambda s: None)
    monkeypatch.setattr(google.requests, "post", fake_post)
    with pytest.raises(requests.HTTPError) as excinfo:
        google.batch_get_message_metadata("tok", ["m1", "m2"])

    assert excinfo.value.response.status_code == 429
    assert "rateLimitExceeded" in excinfo.value.response.text
    assert len(posts) == google.BATCH_RETRY_ATTEMPTS


def test_inner_error_reads_reason_and_retry_after():
    """The body's machine-readable reason + Google's 'Retry after' stamp are
    parsed verbatim; an unparseable body yields no claim at all."""
    from datetime import datetime, timedelta, timezone

    reason, wait = google._inner_error(
        '{"error": {"code": 429, "message": "User-rate limit exceeded.  '
        'Retry after 2020-01-01T00:00:00.000Z", '
        '"errors": [{"reason": "rateLimitExceeded"}]}}')
    assert reason == "ratelimitexceeded"
    assert wait == 1.0  # that stamp is long past → clamped to the floor

    # A stamp ~2 minutes out is clamped to the 65s ceiling (a clock skew can
    # never make the export hang on Google's own retry time).
    soon = (datetime.now(timezone.utc) + timedelta(minutes=2))
    reason, wait = google._inner_error(
        '{"error": {"message": "Retry after ' + soon.isoformat() + '", '
        '"errors": [{"reason": "rateLimitExceeded"}]}}')
    assert wait == 65.0

    # No errors[] list → the status field answers.
    assert google._inner_error(
        '{"error": {"status": "RESOURCE_EXHAUSTED"}}')[0] == \
        "resource_exhausted"
    # Not the JSON error shape → honest empty, never a guess.
    assert google._inner_error("<html>502</html>") == ("", None)


def test_batch_pacing_respects_the_per_second_quota():
    """Gmail meters 250 quota units per user per SECOND and messages.get
    costs 5, so one batch spends BATCH_MAX_INNER * 5 units the instant it
    lands: the chunk must fit the burst bucket and the pause must keep the
    sustained rate legal. Measured live 2026-09-16 — with 100-id batches
    most calls came back partly throttled and the 2024 export ground 20
    minutes through window waits."""
    units_per_batch = google.BATCH_MAX_INNER * 5
    assert units_per_batch <= 250                    # fits the burst bucket
    assert units_per_batch / google.BATCH_PAUSE_S <= 250   # sustained rate
    assert google.BATCH_PAUSE_MAX_S >= google.BATCH_PAUSE_S


def test_batch_get_message_metadata_whole_batch_error_raises(monkeypatch):
    """A non-2xx on the POST itself (transport/permission) raises through
    raise_for_status — the caller's retry/502 path handles it."""
    import pytest
    import requests

    def fake_post(url, *, headers, data, timeout):
        resp = requests.Response()
        resp.status_code = 401
        return resp

    monkeypatch.setattr(google.requests, "post", fake_post)
    with pytest.raises(requests.HTTPError):
        google.batch_get_message_metadata("tok", ["m1"])
