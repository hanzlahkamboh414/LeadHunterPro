"""Email sending accounts API (Phase E2) — Connect Gmail via Google OAuth.

The flow (no Gmail password is ever seen, let alone stored):

    Settings -> Connect Gmail
      -> GET /email-accounts/google/authorize?token=<jwt>   (302 to Google)
      -> Google consent page (the user picks the Gmail + approves)
      -> GET /email-accounts/google/callback?code&state      (302 back to /settings)

The callback verifies the signed state (CSRF + which user), exchanges the
code for tokens, and stores them ENCRYPTED — refresh tokens only ever exist
ciphered at rest and are never returned by any endpoint.

Why authorize takes ``?token=``: the browser NAVIGATES to Google and back —
there is no ``Authorization`` header on a top-level navigation, so the JWT
rides in the query string for this one hop and the state carries the user
identity for the return hop.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.auth.activity import get_activity
from app.auth.dependencies import get_current_user
from app.auth.jwt import decode_access_token
from app.auth.models import User
from app.core.config import settings
from app.email_accounts import google
from app.email_accounts.store import get_email_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email-accounts", tags=["Email Accounts"])


def _spa_settings(params: str) -> str:
    """Back into the SPA at /settings with query params (the flow's exit)."""
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/settings?{params}"


def _configured() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


@router.get("/google/status")
def google_status() -> dict[str, bool]:
    """Whether Gmail OAuth is configured — drives the Settings screen's
    "Connect Gmail" button vs the honest setup notice. Public: leaks nothing
    beyond configured / not."""
    return {"configured": _configured()}


@router.get("/google/authorize")
def google_authorize(token: str = Query(default="")) -> RedirectResponse:
    """Start the Google consent flow — 302 to Google's authorization page.

    The JWT arrives as ``?token=`` (top-level browser navigation has no auth
    header). A signed ``state`` carries the user through the round-trip.
    """
    if not _configured():
        raise HTTPException(
            status_code=503,
            detail="Gmail OAuth is not configured — set GOOGLE_CLIENT_ID and "
                   "GOOGLE_CLIENT_SECRET (see docs/email_oauth_setup.md).",
        )
    payload = decode_access_token(token) if token else None
    user_id = (payload or {}).get("sub") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return RedirectResponse(google.authorize_url(google.make_state(user_id)),
                            status_code=302)


@router.get("/google/callback")
def google_callback(
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
) -> RedirectResponse:
    """Google redirects here after consent. Exchanges the code, stores the
    account, and bounces into the SPA with an honest result banner."""
    if error:
        logger.info("Gmail OAuth callback error from Google: %s", error)
        return RedirectResponse(_spa_settings(f"gmail=error:{error}"), status_code=302)
    user_id = google.verify_state(state)
    if not user_id:
        return RedirectResponse(_spa_settings("gmail=error:expired-state"), status_code=302)
    if not code:
        return RedirectResponse(_spa_settings("gmail=error:missing-code"), status_code=302)
    try:
        tokens = google.exchange_code(code)
    except Exception as exc:  # noqa: BLE001 — Google's error shape varies
        logger.warning("Gmail OAuth code exchange failed: %s", exc)
        return RedirectResponse(_spa_settings("gmail=error:exchange-failed"), status_code=302)

    claims = google.decode_id_token(tokens.get("id_token", ""))
    email = (claims.get("email") or "").strip()
    if not email:
        return RedirectResponse(_spa_settings("gmail=error:no-email-claim"), status_code=302)

    store = get_email_store()
    store.connect(
        user_id, email,
        display_name=(claims.get("name") or ""),
        access_token=tokens.get("access_token", ""),
        refresh_token=tokens.get("refresh_token", ""),
        token_expires_at=str(int(time.time()) + int(tokens.get("expires_in", 3600))),
        scopes=tokens.get("scope", ""),
    )
    get_activity().record(user_id, "", "email_account",
                          detail=f"Gmail connected: {email}")
    logger.info("Gmail OAuth connected %s (user %s)", email, user_id)
    return RedirectResponse(_spa_settings(f"gmail=connected:{email}"), status_code=302)


@router.get("")
def list_accounts(user: User = Depends(get_current_user)) -> list[dict[str, Any]]:
    """The caller's connected sending accounts — tokens NEVER appear."""
    return get_email_store().list_for_user(user.id)


@router.delete("/{account_id}")
def disconnect(account_id: int, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Disconnect one account — its tokens are deleted, not just hidden."""
    store = get_email_store()
    creds = store.get_credentials(account_id, user.id)
    if creds is None:
        raise HTTPException(status_code=404, detail="no such account")
    if not store.delete(account_id, user.id):
        raise HTTPException(status_code=404, detail="no such account")
    get_activity().record(user.id, user.username, "email_account",
                          detail=f"Gmail disconnected: {creds['email']}")
    logger.info("DELETE /email-accounts/%d -> %s disconnected", account_id, creds["email"])
    return {"id": account_id, "deleted": True}


@router.post("/{account_id}/send-test")
def send_test(account_id: int, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Send a test email FROM the connected account TO ITSELF — the
    end-to-end proof (OAuth token -> Gmail API -> delivered) without
    touching anyone else's inbox."""
    store = get_email_store()
    creds = store.get_credentials(account_id, user.id)
    if creds is None:
        raise HTTPException(status_code=404, detail="no such account")
    if not creds["access_token"] and not creds["refresh_token"]:
        # Undecryptable tokens (key rotated) — honest reset, not a fake send.
        raise HTTPException(status_code=409, detail="account tokens unreadable — reconnect Gmail")
    try:
        google.send_gmail(
            creds["access_token"],
            to=creds["email"],
            from_email=creds["email"],
            subject="LeadHunter Pro — test email",
            body=(f"Hi {user.name or user.username},\n\n"
                  "This is a test email from LeadHunter Pro. Your Gmail "
                  "connection is working — emails will send from this "
                  "account.\n\n— LeadHunter Pro"),
        )
    except Exception as exc:  # noqa: BLE001 — Gmail's error shape varies
        logger.warning("Test send via %s failed: %s", creds["email"], exc)
        get_email_store().mark_status(account_id, user.id, "revoked")
        detail = f"Gmail refused the send — reconnect the account ({creds['email']})."
        body = getattr(exc, "response", None)
        if body is not None:
            try:
                g = body.json().get("error", {}).get("message", "")
                if g:
                    detail = f"Gmail: {g[:300]}"
            except Exception:  # noqa: BLE001 — non-JSON body
                pass
        raise HTTPException(status_code=502, detail=detail) from exc
    # A success clears any earlier 'revoked' flag — the account IS healthy.
    get_email_store().mark_status(account_id, user.id, "connected")
    logger.info("POST /email-accounts/%d/send-test -> OK via %s", account_id, creds["email"])
    return {"id": account_id, "sent": True, "to": creds["email"]}
