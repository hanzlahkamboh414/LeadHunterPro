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

Known honesty limits (inherent to the technique, not bugs): image
caching and privacy features like Apple Mail Privacy Protection can
record an open nobody did, and clients that block images record nothing.
The open count is a signal, not a proof.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from app.core.config import settings

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
