"""Token encryption at rest — Fernet (AES-128-CBC + HMAC), from ``cryptography``.

OAuth refresh tokens are long-lived credentials that can send email as the
user, so they are NEVER stored in plaintext. The key comes from
``EMAIL_TOKEN_KEY`` when set, otherwise it is DERIVED from
``AUTH_SECRET_KEY`` — documented trade-off: rotating AUTH_SECRET_KEY makes
stored tokens unreadable (set EMAIL_TOKEN_KEY to decouple the two).
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_fernet: Fernet | None = None


def _fernet_key() -> bytes:
    """The Fernet key (32 url-safe base64 bytes) — explicit or derived."""
    if settings.EMAIL_TOKEN_KEY:
        return settings.EMAIL_TOKEN_KEY.encode()
    # Derive: sha256 over a namespaced secret — deterministic, and a distinct
    # domain from the JWT key so the two never collide by construction.
    digest = hashlib.sha256(
        ("leadhunter-email-tokens:" + settings.AUTH_SECRET_KEY).encode()
    ).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_fernet_key())
    return _fernet


def encrypt_text(plain: str) -> str:
    """Plaintext -> Fernet token string. Empty input stays empty (no token)."""
    if not plain:
        return ""
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_text(token: str) -> str:
    """Fernet token string -> plaintext. Empty stays empty; a token that no
    longer decrypts (key rotated / corrupt) returns "" — callers treat that
    as an expired account, never a crash."""
    if not token:
        return ""
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""
