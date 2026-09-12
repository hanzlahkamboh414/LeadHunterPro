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
import json
import time
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlencode

import requests

from app.core.config import settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

#: gmail.send sends; gmail.readonly reads the INBOX — and nothing else — to
#: detect replies (Phase E4). openid/email/profile identify the connected
#: account without any extra consent screen friction.
OAUTH_SCOPES = ("openid email profile "
                "https://www.googleapis.com/auth/gmail.send "
                "https://www.googleapis.com/auth/gmail.readonly")

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
               from_email: str) -> dict[str, Any]:
    """Send ONE plain-text email via the Gmail API; returns the API response.

    The raw message is RFC 2822 MIME base64url — Gmail's send contract. A
    non-2xx raises :class:`requests.HTTPError`; the caller maps 401/403 to a
    revoked/expired account and 429 to the Phase-E3 backoff.
    """
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = from_email
    msg["Subject"] = subject
    msg.set_content(body)
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
    after:`` query (metadata only — bodies are never fetched), then one
    metadata GET per message for its From/Subject headers. Replies are
    matched by the CALLER against addresses this account actually emailed —
    this function reads nothing else and stores nothing.

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
                    "subject": hdrs.get("subject", "")})
    return out
