"""JWT token creation and decoding for LeadHunter Pro auth."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings


def create_access_token(
    user_id: str, is_admin: bool = False, username: str = "", name: str = "",
    category: str = "both",
) -> str:
    """Create a JWT access token for the given user.

    ``sub`` is the standard subject claim (what ``get_current_user`` reads);
    ``user_id``/``username``/``name``/``is_admin``/``category`` are
    convenience claims the frontend decodes client-side (AuthContext)
    without an extra round-trip.
    """
    expire = datetime.now(timezone.utc) + timedelta(
        hours=settings.AUTH_TOKEN_EXPIRE_HOURS
    )
    payload = {
        "sub": user_id,
        "user_id": user_id,
        "username": username,
        "name": name,
        "is_admin": is_admin,
        "admin": is_admin,
        "category": category if category in ("emails", "phones", "both") else "both",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.AUTH_SECRET_KEY, algorithm="HS256")


def decode_access_token(token: str) -> dict | None:
    """Decode and validate a JWT token. Returns payload or None on failure."""
    try:
        payload = jwt.decode(
            token, settings.AUTH_SECRET_KEY, algorithms=["HS256"]
        )
        return payload
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
