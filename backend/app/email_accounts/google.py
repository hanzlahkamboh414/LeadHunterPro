"""Google/Gmail OAuth client — plain HTTPS, no Google SDK (one less dependency).

Scope: ``gmail.send`` + ``gmail.readonly`` (Phase E4 reply detection) +
openid email/profile (to know WHICH Gmail connected). Both Gmail scopes are
Google RESTRICTED scopes: the app runs in the operator's own Google Cloud
project, and Testing mode (100 test users, 7-day refresh token expiry) is
fine for private use — production/public onboarding needs Google's
verification (the operator checklist lives in docs/google_verification.md;
the public privacy/terms pages it requires are Phase E6).

Accounts connected BEFORE Phase E4 hold only ``gmail.send`` — sending keeps
working, but reply detection is skipped for them until they reconnect (the
granted scopes are stored on the account row so this is knowable, not
guessed).

All network I/O lives in module-level functions so tests can monkeypatch
them (no real Google call ever runs in the suite).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlencode

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

#: Gmail's per-API batch endpoint (the global /batch was retired; the
#: per-service one lives on). One multipart POST carries up to 100 inner
#: requests — measured live 2026-09-15: 100 metadata GETs in ONE 1.2s call
#: vs ~15-30s as 100 parallel single GETs. That difference is what makes a
#: 6,000-message export finish in ~2 minutes instead of tripping nginx's
#: 10-minute timeout (the observed 504s on main).
GMAIL_BATCH_URL = "https://gmail.googleapis.com/batch/gmail/v1"

#: Ids per inner batch request. Gmail allows 100, but the per-user limiter
#: is a BURST bucket: measured live 2026-09-16, a 100-id batch (500 quota
#: units at messages.get = 5) had 14-34 ids rejected outright on nearly
#: every call while a 0.7s pause still let whole pages through — i.e. the
#: bucket sits well under 500 units, so a full batch can never fit it
#: however long we wait between calls. 25 ids = 125 units, which clears the
#: bucket every time; pacing then keeps the sustained rate legal.
BATCH_MAX_INNER = 25

#: Retry rounds for TRANSIENT inner answers. Two flavors are transient:
#: 5xx "Backend Error" (Gmail's batch backend throws them when batches
#: arrive back-to-back — observed live 2026-09-15: a whole 100-id page
#: 503'd, the same ids succeeded seconds later) and 429/403 rate-limit
#: answers (the per-user throttle, which Google's own message dates with a
#: "Retry after <time>" stamp when it knows). Each round re-requests ONLY
#: the ids that failed, with a growing delay.
BATCH_RETRY_ATTEMPTS = 4
BATCH_RETRY_BACKOFF_S = (2.0, 5.0, 15.0, 30.0)

#: Backoff when the round was THROTTLED (429/rate-limit). Different ladder
#: from the 5xx one on purpose: a throttle is a per-minute budget, so short
#: waits just burn rounds — 20/45/60s rides a window out inside the batch
#: call instead of aborting a page's worth of work.
BATCH_THROTTLE_BACKOFF_S = (20.0, 45.0, 60.0)

#: Inner-batch error reasons Google itself calls transient, read from the
#: body's ``error.errors[].reason`` — never guessed from the status line.
#: A 403 carrying anything else (e.g. ``insufficientPermissions``) is a real
#: permission failure and raises immediately instead of looping.
_BATCH_TRANSIENT_REASONS = frozenset({
    "ratelimitexceeded", "userratelimitexceeded", "quotaexceeded",
    "backenderror", "internalerror",
})

#: Pause between consecutive batch POSTs: 25 ids = 125 quota units, and the
#: per-user budget is 250 units/second, so 0.6s keeps the sustained rate
#: (~208 units/s) inside it with room for call latency. Measured live
#: 2026-09-16: at 100-id batches the 2024 export crawled 20 minutes through
#: throttle windows and would have 504'd behind nginx's 10-minute proxy
#: timeout; 100-id batches also spent most calls partly rejected. When a
#: batch gets throttled anyway the pause grows toward
#: :data:`BATCH_PAUSE_MAX_S` — the next chunk must not trip it again.
BATCH_PAUSE_S = 0.6
BATCH_PAUSE_MAX_S = 2.0

#: gmail.send sends; gmail.readonly reads mail (reply detection + the
#: inbox screen); gmail.modify marks read/unread, stars, trashes — the
#: "act on a message like the Gmail site" actions. openid/email/profile
#: identify the connected account without extra consent friction.
OAUTH_SCOPES = ("openid email profile "
                "https://www.googleapis.com/auth/gmail.send "
                "https://www.googleapis.com/auth/gmail.readonly "
                "https://www.googleapis.com/auth/gmail.modify")

#: The signed `state` lifetime — the CSRF window for the OAuth round-trip.
STATE_TTL_SECONDS = 600


def redirect_uri() -> str:
    """The exact URI registered in the Google Cloud console."""
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/email-accounts/google/callback"


def _state_secret() -> bytes:
    """HMAC key for the OAuth state — the app's own secret, namespaced."""
    raw = f"leadhunter-oauth-state:{settings.AUTH_SECRET_KEY}"
    return hashlib.sha256(raw.encode()).digest()


def make_state(user_id: str, *, now: float | None = None) -> str:
    """`user_id:expiry:hmac` — the callback proves the round-trip started
    here (CSRF) and names the user it belongs to (no cookie needed — the
    browser navigates away to Google and back without our JWT header)."""
    ts = int(now if now is not None else time.time())
    expiry = ts + STATE_TTL_SECONDS
    payload = f"{user_id}:{expiry}"
    sig = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_state(state: str) -> str | None:
    """The user_id the state was issued to, or None (bad signature/expired)."""
    parts = (state or "").split(":")
    if len(parts) != 3:
        return None
    user_id, expiry, sig = parts
    payload = f"{user_id}:{expiry}"
    expected = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        if int(expiry) < time.time():
            return None  # expired — restart the flow
    except ValueError:
        return None
    return user_id or None


def authorize_url(state: str) -> str:
    """The Google consent page URL (offline access → refresh token)."""
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "access_type": "offline",   # ask for a refresh token
        "prompt": "consent",        # ... every time (re-connect refreshes it)
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict[str, Any]:
    """Authorization code -> tokens (HTTPS POST). Raises for Google errors —
    the caller maps them to an honest error redirect."""
    resp = requests.post(GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Refresh token -> a fresh access token (expires_in seconds)."""
    resp = requests.post(GOOGLE_TOKEN_URL, data={
        "refresh_token": refresh_token,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def decode_id_token(id_token: str) -> dict[str, Any]:
    """The id_token payload (claims) from the TOKEN ENDPOINT response.

    No signature verification: this token arrived directly from Google over
    TLS in exchange for the code — we are Google's client, not a browser
    forwarding an untrusted token. (Verifying would need Google's JWKS; it
    defends a threat that doesn't exist in this flow.)
    """
    try:
        payload_b64 = id_token.split(".")[1]
        # Restore the padding urlsafe_b64decode needs.
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        return claims if isinstance(claims, dict) else {}
    except (IndexError, ValueError, binascii.Error):
        return {}


def send_gmail(access_token: str, *, to: str, subject: str, body: str,
               from_email: str,
               cc: str = "", bcc: str = "",
               in_reply_to: str = "", references: str = "",
               tracking_url: str = "") -> dict[str, Any]:
    """Send ONE plain-text email via the Gmail API; returns the API response.

    The raw message is RFC 2822 MIME base64url — Gmail's send contract. A
    non-2xx raises :class:`requests.HTTPError`; the caller maps 401/403 to a
    revoked/expired account and 429 to the Phase-E3 backoff.

    ``cc``/``bcc`` add the matching headers (the inbox screen's compose).
    ``in_reply_to``/``references`` thread a REPLY into its conversation the
    way the Gmail site does (the client's Message-ID/References of the
    message being answered).

    ``tracking_url`` (campaign sends only) switches the message to
    multipart/alternative: the same plain-text body plus an HTML part
    ending in the 1x1 open-tracking image. Test sends pass no URL — they
    create nothing, so there is nothing to track.
    """
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = from_email
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    if tracking_url:
        html_body = (
            html.escape(body)
            .replace("\r\n", "\n")
            .replace("\n\n", "<br><br>")
            .replace("\n", "<br>")
            + f'\n<img src="{tracking_url}" width="1" height="1" alt="">'
        )
        msg.add_alternative(html_body, subtype="html")
    raw = _b64url(msg.as_bytes())
    resp = requests.post(
        GMAIL_SEND_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        json={"raw": raw},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def parse_from(from_header: str) -> str:
    """The bare email address from an RFC-2822 From header, lowercased —
    ``"Jane <jane@acme.com>"`` -> ``"jane@acme.com"``."""
    _, addr = parseaddr(from_header or "")
    return addr.strip().lower()


def list_inbox_senders(access_token: str, *, after_unix: int,
                       limit: int = 50) -> list[dict[str, Any]]:
    """Who wrote to this inbox since ``after_unix`` (epoch seconds).

    Reply detection (Phase E4): one ``messages.list`` with an ``in:inbox
    after:`` query (metadata headers — full bodies are never fetched), then
    one metadata GET per message for its From/Subject headers and the
    response's short snippet (needed to read a DSN's failed address; still
    no body fetch). Replies are matched by the CALLER against addresses
    this account actually emailed — this function reads nothing else and
    stores nothing.

    A non-2xx raises :class:`requests.HTTPError`; the scheduler treats reply
    detection as best-effort (a failure never pauses a campaign).
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(
        GMAIL_MESSAGES_URL,
        headers=headers,
        params={"q": f"in:inbox after:{int(after_unix)}",
                "maxResults": min(int(limit), 50)},
        timeout=15,
    )
    resp.raise_for_status()
    ids = [m["id"] for m in resp.json().get("messages", [])]
    out: list[dict[str, Any]] = []
    for mid in ids[: int(limit)]:
        r = requests.get(
            f"{GMAIL_MESSAGES_URL}/{mid}",
            headers=headers,
            params={"format": "metadata",
                    "metadataHeaders": ["From", "Subject"]},
            timeout=15,
        )
        r.raise_for_status()
        hdrs = {h["name"].lower(): h["value"]
                for h in r.json().get("payload", {}).get("headers", [])}
        out.append({"from": hdrs.get("from", ""),
                    "subject": hdrs.get("subject", ""),
                    "snippet": r.json().get("snippet", "")})
    return out


# ---------------------------------------------------------------------------
# Gmail-inbox client (Phase E7) — the connected account's mail, read AND
# acted on, the way the Gmail site does. Still plain HTTPS + requests; every
# function stays module-level so tests monkeypatch them.
# ---------------------------------------------------------------------------

#: The system labels the inbox screen folders map onto.
FOLDER_LABELS = {
    "inbox": "INBOX",
    "sent": "SENT",
    "starred": "STARRED",
    "trash": "TRASH",
}


def list_message_ids(access_token: str, *, label: str = "INBOX", q: str = "",
                     page_token: str = "", limit: int = 50) -> dict[str, Any]:
    """One page of message ids for a label/search (Gmail ``messages.list``).

    ``q`` is Gmail's own search syntax, passed straight through (the inbox
    screen's search box is Gmail search). Returns
    ``{"ids", "next_page_token", "total_estimate"}``.
    """
    params: dict[str, Any] = {"maxResults": max(1, min(int(limit), 100))}
    if label:
        params["labelIds"] = label
    if q:
        params["q"] = q
    if page_token:
        params["pageToken"] = page_token
    resp = requests.get(
        GMAIL_MESSAGES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "ids": [m["id"] for m in data.get("messages", [])],
        "next_page_token": data.get("nextPageToken", ""),
        "total_estimate": int(data.get("resultSizeEstimate", 0)),
    }


def _decode_b64url(data: str) -> str:
    """Gmail's body data (base64url, padding stripped) -> text."""
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")


def _walk_parts(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """(text_body, html_body, attachments) from one ``format=full`` payload.

    Walks multipart trees depth-first; the FIRST text/plain and text/html
    parts win (Gmail puts the visible body first; later alternatives are
    quoting cruft). A part with a filename is an attachment whatever its
    mime type.
    """
    text = html_body = ""
    attachments: list[dict[str, Any]] = []
    stack = [payload]
    while stack:
        part = stack.pop(0)
        filename = part.get("filename") or ""
        body = part.get("body", {}) or {}
        if filename and body.get("attachmentId"):
            attachments.append({
                "attachment_id": body["attachmentId"],
                "filename": filename,
                "mime_type": part.get("mimeType", "application/octet-stream"),
                "size": int(body.get("size", 0)),
            })
            continue
        if not text and part.get("mimeType") == "text/plain":
            text = _decode_b64url(body.get("data", ""))
        elif not html_body and part.get("mimeType") == "text/html":
            html_body = _decode_b64url(body.get("data", ""))
        stack = list(part.get("parts", []) or []) + stack
    return text, html_body, attachments


#: Header names get_message parses (lowercased in the result dict).
_MESSAGE_HEADERS = ("from", "to", "cc", "subject", "date", "message-id",
                    "in-reply-to", "references")


def get_message(access_token: str, message_id: str,
                *, metadata_only: bool = False) -> dict[str, Any]:
    """One message, parsed (Gmail ``messages.get`` ``format=full``).

    Returns ``{"id", "thread_id", "snippet", "headers", "text", "html",
    "attachments", "labels"}`` — headers lowercased (from/to/cc/subject/
    date/message-id/in-reply-to/references), body as text/plain with the
    text/html alternative kept for the sandboxed reader, attachments as
    ``{attachment_id, filename, mime_type, size}``. ``metadata_only`` skips
    the body walk (list rows only need headers + snippet).
    """
    params = {"format": "metadata",
              "metadataHeaders": [h.title() for h in _MESSAGE_HEADERS]} \
        if metadata_only else {"format": "full"}
    r = requests.get(
        f"{GMAIL_MESSAGES_URL}/{message_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    payload = data.get("payload", {}) or {}
    headers = {h["name"].lower(): h["value"]
               for h in payload.get("headers", [])}
    out: dict[str, Any] = {
        "id": data.get("id", message_id),
        "thread_id": data.get("threadId", ""),
        "snippet": data.get("snippet", ""),
        "headers": {k: headers.get(k, "") for k in _MESSAGE_HEADERS},
        "labels": data.get("labelIds", []) or [],
    }
    if metadata_only:
        out.update({"text": "", "html": "", "attachments": []})
        return out
    text, html_body, attachments = _walk_parts(payload)
    out.update({"text": text, "html": html_body, "attachments": attachments})
    return out


#: Google's own "Retry after 2026-09-16T02:12:34.135Z" stamp inside a
#: throttle message — the authoritative wait, when present.
_RETRY_AFTER_RE = re.compile(r"retry after\s+([0-9][0-9T:.\-+Z]*)", re.I)


def _inner_error(body: str) -> tuple[str, float | None]:
    """``(reason, retry_after_seconds)`` from an inner batch error body.

    ``reason`` is Google's own machine-readable code (lowercased), read from
    ``error.errors[].reason`` — falling back to ``error.status`` (e.g.
    ``RESOURCE_EXHAUSTED``) when the list is absent. The wait is parsed from
    the human message ("Retry after <ISO time>") and clamped to
    ``[1, 65]`` seconds: long enough to ride out a per-minute window, short
    enough that a clock skew can never hang the export. Both are ``""``/
    ``None`` when the body is not the JSON error shape (never guessed).
    """
    try:
        err = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        return "", None
    reasons = [e.get("reason", "") for e in (err.get("errors") or [])]
    reason = (reasons[0] if reasons and reasons[0] else err.get("status", ""))
    match = _RETRY_AFTER_RE.search(err.get("message", "") or "")
    wait: float | None = None
    if match:
        try:
            when = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            wait = max(1.0, min(
                (when - datetime.now(timezone.utc)).total_seconds(), 65.0))
        except ValueError:
            wait = None
    return reason.lower(), wait


def batch_get_message_metadata(access_token: str,
                               message_ids: list[str]) -> list[dict[str, Any]]:
    """Metadata for many messages via ONE batched HTTP call (or a few).

    Same result shape as :func:`get_message` with ``metadata_only=True``,
    one dict per SUCCESSFUL message id, in request order. Up to
    :data:`BATCH_MAX_INNER` ids per inner request; more ids are split into
    consecutive batch POSTs.

    Failure contract (the export's honest-skip vs retry seam):
      * a whole-batch transport error (non-2xx on the POST itself) RAISES —
        the caller's retry/backoff handles it;
      * inner 429 / rate-limit 403 answers are TRANSIENT and re-requested
        only for the ids that got them, waiting Google's own "Retry after"
        stamp when it gave one — the per-user throttle answers this way
        (observed live 2026-09-15: a whole year of sent mail 429'd mid-scan
        and the export died 502 after the caller's short burst chain).
        Ids still throttled after every round RAISE, never vanish: a
        throttled page means the addresses behind them are unknown, and an
        export that silently drops a few hundred addresses is worse than an
        honest retryable error;
      * a 403 that is NOT one of Google's rate-limit reasons (a real
        permission failure) raises immediately — retrying cannot fix it;
      * inner 5xx answers are transient too; only ids STILL failing after
        every round are skipped (and logged), since a lone flaky message
        must not fail the whole export.
    """
    from email.parser import BytesParser

    out: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {access_token}"}
    wanted = [h.title() for h in _MESSAGE_HEADERS]

    def _one_batch(chunk: list[str]) -> tuple[list[dict[str, Any]], list[str],
                                              list[str], float | None]:
        """One batch POST -> (metas, 5xx ids, throttled ids, wait hint)."""
        boundary = f"leadhunter-{uuid.uuid4().hex}"
        parts = [
            f"--{boundary}\r\n"
            "Content-Type: application/http\r\n"
            f"Content-ID: <id:{mid}>\r\n\r\n"
            f"GET /gmail/v1/users/me/messages/{mid}"
            "?format=metadata"
            + "".join(f"&metadataHeaders={h}" for h in wanted)
            + "\r\n"
            for mid in chunk
        ]
        resp = requests.post(
            GMAIL_BATCH_URL,
            headers={**headers,
                     "Content-Type": f"multipart/mixed; boundary={boundary}"},
            data="".join(parts) + f"--{boundary}--\r\n",
            timeout=60,
        )
        resp.raise_for_status()

        # Parse the multipart reply as a MIME message; each application/http
        # part is a full inner HTTP response (status line + JSON body).
        ctype = resp.headers.get("Content-Type", "")
        mime = BytesParser().parsebytes(
            f"Content-Type: {ctype}\r\n\r\n".encode() + resp.content)
        metas: list[dict[str, Any]] = []
        retryable: set[str] = set(chunk)      # 5xx — transient, per-id
        throttled: set[str] = set()           # 429/rate-limit — transient too
        wait_hint: float | None = None
        for part in mime.walk():
            if part.get_content_type() != "application/http":
                continue
            # Response parts echo the request's id: <response-id:m>. That
            # maps a failed inner answer back to the id that was asked for
            # (the JSON body of an error carries no usable id).
            cid = (part.get("Content-ID") or "").strip("<>")
            req_mid = cid.removeprefix("response-id:")
            inner = part.get_payload(decode=True).decode("utf-8", "replace")
            # "HTTP/1.1 200 OK\r\n<headers>\r\n\r\n<json>" — split status,
            # headers and body on the first blank line.
            head, _, body = inner.partition("\r\n\r\n")
            lines = head.split("\r\n")
            try:
                status = int(lines[0].split()[1])
            except (IndexError, ValueError):
                continue  # unparseable part — skip, never fabricate
            if status in (403, 429):
                reason, wait = _inner_error(body)
                if status == 429 or reason in _BATCH_TRANSIENT_REASONS:
                    # The per-user throttle, not a failure of this request:
                    # the id goes back for the next round (it stays in
                    # ``retryable`` only if the id was in this chunk — track
                    # it separately so a leftover is never silently skipped).
                    retryable.discard(req_mid)
                    throttled.add(req_mid)
                    if wait is not None:
                        wait_hint = max(wait_hint or 0.0, wait)
                    continue
                bad = requests.Response()
                bad.status_code = status
                bad._content = body.encode("utf-8", "replace")
                raise requests.HTTPError(
                    f"{status} inner batch response for a message",
                    response=bad)
            if status != 200:
                # 4xx (404 = deleted message) is permanent — drop it from
                # the retry set; 5xx stays for the retry rounds below.
                if status < 500:
                    retryable.discard(req_mid)
                continue
            try:
                data = json.loads(body)
            except ValueError:
                continue
            mid = data.get("id", "") or req_mid
            retryable.discard(mid)
            payload = data.get("payload", {}) or {}
            hdrs = {h["name"].lower(): h["value"]
                    for h in payload.get("headers", [])}
            metas.append({
                "id": mid,
                "thread_id": data.get("threadId", ""),
                "snippet": data.get("snippet", ""),
                "headers": {k: hdrs.get(k, "") for k in _MESSAGE_HEADERS},
                "labels": data.get("labelIds", []) or [],
                "text": "", "html": "", "attachments": [],
            })
        return metas, list(retryable), list(throttled), wait_hint

    pause = BATCH_PAUSE_S
    for start in range(0, len(message_ids), BATCH_MAX_INNER):
        if start:
            time.sleep(pause)
        chunk = message_ids[start:start + BATCH_MAX_INNER]
        pending = chunk
        failed: list[str] = []
        throttled: list[str] = []
        throttled_this_chunk = False
        for attempt in range(BATCH_RETRY_ATTEMPTS):
            metas, failed, throttled, wait = _one_batch(pending)
            out.extend(metas)
            if not failed and not throttled:
                break
            throttled_this_chunk = throttled_this_chunk or bool(throttled)
            ladder = (BATCH_THROTTLE_BACKOFF_S if throttled
                      else BATCH_RETRY_BACKOFF_S)
            delay = (wait if wait is not None
                     else ladder[min(attempt, len(ladder) - 1)])
            if throttled:
                logger.warning("gmail batch: %d ids throttled (round %d) — "
                               "waiting %.0fs", len(throttled), attempt + 1,
                               delay)
            time.sleep(delay)
            pending = failed + throttled
        if throttled:
            # Never skip a throttled id: the addresses behind them are simply
            # unknown. Raise with the inner body so the caller's window-length
            # retry can wait the per-minute quota out and ask for the page
            # again (a few duplicate ids cost nothing — the export dedupes).
            bad = requests.Response()
            bad.status_code = 429
            bad._content = json.dumps({
                "error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                          "message": "User-rate limit exceeded — batch "
                                     "throttled after all retry rounds",
                          "errors": [{"reason": "rateLimitExceeded"}]},
            }).encode()
            raise requests.HTTPError(
                f"429 inner batch throttle: {len(throttled)} ids still "
                f"throttled after {BATCH_RETRY_ATTEMPTS} rounds",
                response=bad)
        if failed:
            logger.warning("gmail batch: %d ids still failing after %d "
                           "rounds, skipped", len(failed),
                           BATCH_RETRY_ATTEMPTS)
        if throttled_this_chunk:
            # A throttled chunk means we are pacing too fast for this
            # mailbox — slow the NEXT chunk down instead of tripping it
            # again (a full export is dozens of chunks: one slow chunk
            # beats 63 retry storms).
            pause = min(pause + 1.0, BATCH_PAUSE_MAX_S)
    return out


def get_attachment(access_token: str, message_id: str,
                   attachment_id: str) -> dict[str, Any]:
    """One attachment's bytes (Gmail ``attachments.get``) + its filename
    hint. The caller sets the download response headers."""
    r = requests.get(
        f"{GMAIL_MESSAGES_URL}/{message_id}/attachments/{attachment_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    raw = data.get("data", "")
    padded = raw + "=" * (-len(raw) % 4)
    return {
        "data": base64.urlsafe_b64decode(padded),
        "size": int(data.get("size", 0)),
    }


def modify_message(access_token: str, message_id: str, *,
                   add_labels: tuple[str, ...] = (),
                   remove_labels: tuple[str, ...] = ()) -> dict[str, Any]:
    """Add/remove labels on one message (Gmail ``messages.modify``) — the
    mark-read/unread, star, trash and archive actions. Requires the
    ``gmail.modify`` scope (older connections must reconnect once)."""
    resp = requests.post(
        f"{GMAIL_MESSAGES_URL}/{message_id}/modify",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"addLabelIds": list(add_labels),
              "removeLabelIds": list(remove_labels)},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
