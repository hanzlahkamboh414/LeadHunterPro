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
    name: str = Field("", max_length=100)
    #: P3 signup question — which vertical is this account for?
    #: "emails" | "phones" | "both" (default both; anything else -> both).
    category: str = Field("both", max_length=10)


class LoginRequest(BaseModel):
    username: str
    password: str


class ResetPasswordRequest(BaseModel):
    username: str
    new_password: str = Field(..., min_length=4, max_length=100)


class AuthOut(BaseModel):
    user_id: str
    username: str
    name: str = ""
    is_admin: bool = False
    category: str = "both"
    token: str


class MeOut(BaseModel):
    user_id: str
    username: str
    name: str = ""
    email: str
    is_admin: bool
    category: str = "both"


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
            name=body.name,
            category=body.category,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    token = create_access_token(
        user.id, user.is_admin, username=user.username, name=user.name,
        category=user.category,
    )
    logger.info("Signup: user %s created (category=%s)", user.username, user.category)
    get_activity().record(user.id, user.username, "signup")
    return AuthOut(
        user_id=user.id,
        username=user.username,
        name=user.name,
        is_admin=user.is_admin,
        category=user.category,
        token=token,
    )


@router.post("/login", response_model=AuthOut)
def login(body: LoginRequest) -> AuthOut:
    """Authenticate and return a JWT token."""
    store = _get_store()
    user = store.verify_password(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = create_access_token(
        user.id, user.is_admin, username=user.username, name=user.name,
        category=user.category,
    )
    logger.info("Login: user %s", user.username)
    get_activity().record(user.id, user.username, "login")
    return AuthOut(
        user_id=user.id,
        username=user.username,
        name=user.name,
        is_admin=user.is_admin,
        category=user.category,
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


@router.get("/mode")
def auth_mode() -> dict:
    """PUBLIC — the frontend boot check: is the login page on or off?

    The admin can turn login auth off from the admin panel; the site then
    opens straight into the normal user UI.
    """
    from app.auth.settings import get_settings

    return {"auth_enabled": get_settings().auth_enabled()}


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)) -> MeOut:
    """Return the current authenticated user's info."""
    return MeOut(
        user_id=user.id,
        username=user.username,
        name=user.name,
        email=user.email,
        is_admin=user.is_admin,
        category=user.category,
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
