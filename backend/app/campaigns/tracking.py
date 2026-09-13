"""Open tracking for campaign emails (Phase E5 polish).

Every campaign send carries an invisible 1x1 transparent GIF whose URL
names the send row: when the recipient's mail client loads the image,
GET /campaigns/track/{token} fires and the store records the open (first
open time + total count). That is ALL it records — no IP, no user agent,
no content; the token is an HMAC-signed send id, so a random or forged
URL marks nothing.

Self-hosted on purpose (FREE rule): the pixel is served by this backend,
never by a third-party tracker, and the only data it produces is "this
email was opened, now, for the Nth time" — which is exactly what the
privacy policy says. Honest by construction.

An <img> request carries NO viewer identity, so two glitches of the
technique are filtered by TIME, not by identity:
* the sender opening their own Sent copy right after sending (to see
  how it looks) fires the pixel too — a fire within OPEN_SEND_GRACE_S
  of the send itself is treated as that self-check and not counted;
* mail clients (Gmail's image proxy worst of all) re-fetch the pixel
  several times for ONE view — a fire within OPEN_DEDUPE_S of the last
  COUNTED open is the same view and not counted either.

Known honesty limits (inherent to the technique, not bugs): image
caching and privacy features like Apple Mail Privacy Protection can
record an open nobody did, clients that block images record nothing,
and a sender opening their Sent copy AFTER the grace window still
counts — no pixel tracker on earth can tell those apart. The open count
is a signal, not a proof.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime

from app.core.config import settings

#: A pixel fire this soon after the SEND is the sender opening their own
#: Sent copy (the mail client re-fetches remote images for it), not the
#: recipient — don't count it.
OPEN_SEND_GRACE_S = 300

#: A pixel fire this soon after the last COUNTED open is the same view
#: re-fetched (Gmail's image proxy requests the pixel repeatedly per
#: single open — without this, one view showed as 3x).
OPEN_DEDUPE_S = 3600


def _gap_seconds(anchor: str, now: str) -> float | None:
    """Seconds from ``anchor`` to ``now``, or None when either timestamp
    can't be parsed. Callers treat None as "no evidence" and count the
    open — a bad timestamp must never hide a real one."""
    try:
        return (datetime.fromisoformat(now) - datetime.fromisoformat(anchor)
                ).total_seconds()
    except (ValueError, TypeError):
        return None


def is_sender_selfcheck(sent_at: str, now: str) -> bool:
    """True when this pixel fire is within the post-send grace — almost
    certainly the SENDER viewing their own Sent copy, not the recipient."""
    gap = _gap_seconds(sent_at, now)
    return gap is not None and gap < OPEN_SEND_GRACE_S


def is_repeat_view(last_open_at: str, now: str) -> bool:
    """True when this pixel fire lands within the dedupe window of the
    last COUNTED open — the same view re-fetched by the mail client's
    image proxy. An empty anchor (never counted) is never a repeat."""
    if not last_open_at:
        return False
    gap = _gap_seconds(last_open_at, now)
    return gap is not None and gap < OPEN_DEDUPE_S

#: The classic 43-byte 1x1 transparent GIF.
PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _secret() -> bytes:
    """HMAC key for the pixel token — the app's own secret, namespaced
    (same derivation pattern as the OAuth state)."""
    raw = f"leadhunter-open-track:{settings.AUTH_SECRET_KEY}"
    return hashlib.sha256(raw.encode()).digest()


def pixel_token(send_id: int) -> str:
    """``{send_id}:{hmac}`` — names one send row, unforgeable."""
    sig = hmac.new(_secret(), str(send_id).encode(), hashlib.sha256).hexdigest()
    return f"{send_id}:{sig}"


def parse_token(token: str) -> int | None:
    """The send id a token names, or None (bad shape/signature)."""
    parts = (token or "").split(":")
    if len(parts) != 2:
        return None
    try:
        send_id = int(parts[0])
    except ValueError:
        return None
    expected = hmac.new(_secret(), str(send_id).encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(parts[1], expected):
        return None
    return send_id


def pixel_url(send_id: int) -> str:
    """The image URL embedded in a campaign send (public, no auth — an
    <img> tag carries no JWT; the signature is the auth)."""
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/api/v1/campaigns/track/{pixel_token(send_id)}.png"
