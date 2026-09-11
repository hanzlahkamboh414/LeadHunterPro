"""FastAPI dependencies for auth — get_current_user, require_admin."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app.auth.jwt import decode_access_token
from app.auth.models import User, UserStore


def _user_store() -> UserStore:
    """Lazy singleton — same pattern as JobManager/LeadResearchStore."""
    if not hasattr(_user_store, "_instance"):
        _user_store._instance = UserStore()  # type: ignore[attr-defined]
    return _user_store._instance


def get_current_user(request: Request) -> User:
    """Extract and validate JWT from Authorization header.

    Returns the User if valid. Raises 401 if missing/invalid/expired.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Not authenticated — provide Authorization: Bearer <token>",
        )
    token = auth_header[7:]  # strip "Bearer "
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("sub", "")
    store = _user_store()
    user = store.get_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Require the current user to be an admin."""
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Admin access required",
        )
    return user
