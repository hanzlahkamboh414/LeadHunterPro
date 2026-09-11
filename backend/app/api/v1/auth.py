"""Auth API — signup, login, password reset, me.

All endpoints are PUBLIC (no token required) except GET /me.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.activity import get_activity
from app.auth.dependencies import get_current_user
from app.auth.jwt import create_access_token
from app.auth.models import User, UserStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])

_store: UserStore | None = None


def _get_store() -> UserStore:
    global _store
    if _store is None:
        _store = UserStore()
    return _store


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class SignupRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=30)
    email: str = Field(..., min_length=5, max_length=100)
    password: str = Field(..., min_length=4, max_length=100)


class LoginRequest(BaseModel):
    username: str
    password: str


class ResetPasswordRequest(BaseModel):
    username: str
    new_password: str = Field(..., min_length=4, max_length=100)


class AuthOut(BaseModel):
    user_id: str
    username: str
    is_admin: bool = False
    token: str


class MeOut(BaseModel):
    user_id: str
    username: str
    email: str
    is_admin: bool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/signup", response_model=AuthOut, status_code=201)
def signup(body: SignupRequest) -> AuthOut:
    """Register a new user account."""
    store = _get_store()
    try:
        user = store.create(
            username=body.username,
            email=body.email,
            password=body.password,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    token = create_access_token(user.id, user.is_admin, username=user.username)
    logger.info("Signup: user %s created", user.username)
    get_activity().record(user.id, user.username, "signup")
    return AuthOut(
        user_id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        token=token,
    )


@router.post("/login", response_model=AuthOut)
def login(body: LoginRequest) -> AuthOut:
    """Authenticate and return a JWT token."""
    store = _get_store()
    user = store.verify_password(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = create_access_token(user.id, user.is_admin, username=user.username)
    logger.info("Login: user %s", user.username)
    get_activity().record(user.id, user.username, "login")
    return AuthOut(
        user_id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        token=token,
    )


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest) -> dict:
    """Reset a user's password by username (no email verification)."""
    store = _get_store()
    updated = store.reset_password(body.username, body.new_password)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("Password reset: user %s", body.username)
    return {"success": True, "message": "Password updated"}


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)) -> MeOut:
    """Return the current authenticated user's info."""
    return MeOut(
        user_id=user.id,
        username=user.username,
        email=user.email,
        is_admin=user.is_admin,
    )


@router.post("/logout")
def logout(user: User = Depends(get_current_user)) -> dict:
    """Record the logout in the activity log.

    The JWT itself is stateless — the client discards the token. This endpoint
    exists so the admin activity view can honestly show logouts.
    """
    logger.info("Logout: user %s", user.username)
    get_activity().record(user.id, user.username, "logout")
    return {"success": True}
