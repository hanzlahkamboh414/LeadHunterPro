"""Campaigns API (Phase E3) — create, watch, pause/resume, delete.

The scheduler thread does the sending; these endpoints are the control
surface. Every route is per-user isolated, and creation is honest about
what it excluded (leads already emailed by an earlier campaign are dropped
and reported, never silently re-mailed).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from app.auth.dependencies import (
    get_current_user, get_tenant_context, multi_tenant_enabled,
)
from app.auth.models import User
from app.campaigns import spamcheck
from app.campaigns.scheduler import ensure_access_token, parse_ts
from app.campaigns.store import get_campaign_store
from app.campaigns.templates import render, sample_context
from app.campaigns.tracking import PIXEL_GIF, parse_token
from app.email_accounts import google
from app.email_accounts.store import get_email_store
from app.schemas.campaigns import (
    CampaignCreateIn,
    CampaignCreateOut,
    CampaignDetailOut,
    CampaignOut,
    CampaignTestSendIn,
    CampaignTestSendOut,
    CampaignUpdateIn,
    CampaignsOut,
    SpamCheckIn,
    SpamCheckOut,
    SpamImproveIn,
    SpamImproveOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaigns", tags=["Campaigns"])


def campaign_tenant_id(
    request: Request, user: User = Depends(get_current_user),
) -> str | None:
    """Check the current membership before campaign/account operations."""
    if not multi_tenant_enabled():
        return None
    return get_tenant_context(request, user).tenant_id


def _validate_start_at(start_at: str) -> str:
    dt = parse_ts(start_at)
    if dt is None:
        raise HTTPException(status_code=422, detail="start_at must be ISO-8601")
    return dt.astimezone(timezone.utc).isoformat()


@router.post("", response_model=CampaignCreateOut)
def create_campaign(
    body: CampaignCreateIn, user: User = Depends(get_current_user),
    tenant_id: str | None = Depends(campaign_tenant_id),
) -> dict:
    """Create a scheduled campaign. Leads that were already emailed (any
    earlier campaign of this user) are excluded and the response says how
    many — never silently re-mailed."""
    email_store = get_email_store()
    owned = {a["id"]: a for a in email_store.list_for_user(user.id, tenant_id=tenant_id)}
    # The primary + every extra account must exist, be the caller's, and
    # be connected (E5 multi-account).
    wanted = [body.account_id] + [a for a in body.account_ids
                                  if a != body.account_id]
    if not wanted:
        raise HTTPException(status_code=422,
                            detail="at least one sending account is required")
    if len(wanted) > 5:
        raise HTTPException(status_code=422,
                            detail="at most 5 sending accounts per campaign")
    for aid in wanted:
        account = owned.get(aid)
        if account is None:
            raise HTTPException(status_code=404,
                                detail="no such connected account")
        if account["status"] != "connected":
            raise HTTPException(
                status_code=409,
                detail=f"account {account['email']} is {account['status']} — "
                       f"reconnect it first",
            )
    if body.delay_min_s > body.delay_max_s:
        raise HTTPException(status_code=422,
                            detail="delay_min_s must be <= delay_max_s")

    start_at = _validate_start_at(body.start_at)

    store = get_campaign_store()
    already = store.already_sent_emails(user.id, body.emails, tenant_id=tenant_id)
    emails = [e for e in body.emails if e not in already]
    if not emails:
        raise HTTPException(
            status_code=422,
            detail=f"all {len(already)} leads were already emailed by an "
                   f"earlier campaign — nothing to send",
        )
    campaign = store.create(
        user.id, account_id=body.account_id, name=body.name.strip(),
        subject=body.subject, body=body.body, emails=emails,
        start_at=start_at, daily_limit=body.daily_limit,
        delay_min_s=body.delay_min_s, delay_max_s=body.delay_max_s,
        followups=[fu.model_dump() for fu in body.followups],
        account_ids=wanted[1:],
        ai_personalize=body.ai_personalize,
        tenant_id=tenant_id,
    )
    campaign["account_email"] = owned[body.account_id]["email"]
    campaign["account_emails"] = [owned[a]["email"] for a in wanted]
    logger.info("POST /campaigns -> %s (%d leads, %d excluded as already-sent, "
                "%d accounts, ai_personalize=%s)",
                campaign["name"], len(emails), len(already), len(wanted),
                body.ai_personalize)
    return {"campaign": campaign, "excluded": len(already)}


@router.post("/test-send", response_model=CampaignTestSendOut)
def campaign_test_send(
    body: CampaignTestSendIn, user: User = Depends(get_current_user),
    tenant_id: str | None = Depends(campaign_tenant_id),
) -> dict:
    """Send the DRAFT pitch to your own address — the spam check. The drafted
    subject/body render with a sample lead, then go out immediately via the
    chosen account. Creates nothing: no campaign, no send row, no CRM event,
    and the recipient is never counted as already-emailed (so you can test as
    often as you like without polluting future campaigns)."""
    email_store = get_email_store()
    account = next(
        (a for a in email_store.list_for_user(user.id, tenant_id=tenant_id)
         if a["id"] == body.account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail="no such connected account")
    if account["status"] != "connected":
        raise HTTPException(
            status_code=409,
            detail=f"account {account['email']} is {account['status']} — "
                   f"reconnect it first",
        )
    creds = email_store.get_credentials(body.account_id, user.id, tenant_id=tenant_id)
    if creds is None or (not creds["access_token"]
                         and not creds["refresh_token"]):
        # Undecryptable tokens (key rotated) — honest reset, not a fake send.
        raise HTTPException(status_code=409,
                            detail="account tokens unreadable — reconnect Gmail")

    ctx = sample_context()
    subject = render(body.subject, ctx)
    email_body = render(body.body, ctx)

    access_token = ensure_access_token(
        email_store, account_id=body.account_id, user_id=user.id,
        creds=creds, now=datetime.now(timezone.utc), tenant_id=tenant_id)
    if not access_token:
        email_store.mark_status(body.account_id, user.id, "revoked", tenant_id=tenant_id)
        raise HTTPException(status_code=409,
                            detail="token refresh failed — reconnect the account")

    try:
        google.send_gmail(
            access_token, to=body.to_email, subject=subject, body=email_body,
            from_email=creds["email"],
        )
    except Exception as exc:  # noqa: BLE001 — Gmail's error shape varies
        logger.warning("Campaign test send via %s failed: %s",
                       creds["email"], exc)
        email_store.mark_status(body.account_id, user.id, "revoked", tenant_id=tenant_id)
        detail = f"Gmail refused the send — reconnect the account ({creds['email']})."
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                g = resp.json().get("error", {}).get("message", "")
                if g:
                    detail = f"Gmail: {g[:300]}"
            except Exception:  # noqa: BLE001 — non-JSON body
                pass
        raise HTTPException(status_code=502, detail=detail) from exc

    # A success clears any earlier 'revoked' flag — the account IS healthy.
    email_store.mark_status(body.account_id, user.id, "connected", tenant_id=tenant_id)
    logger.info("POST /campaigns/test-send -> %s via %s",
                body.to_email, creds["email"])
    return {"sent": True, "to": body.to_email, "from_email": creds["email"],
            "subject": subject}


@router.post("/spam-check", response_model=SpamCheckOut)
def spam_check(body: SpamCheckIn,
               user: User = Depends(get_current_user),
               tenant_id: str | None = Depends(campaign_tenant_id)) -> dict:
    """How spammy does this pitch look? The AI reads the rendered email as
    a deliverability expert (score, plain-words summary, findings with
    fixes) and the rules engine always runs underneath — a blended
    verdict, rules-only when the AI is down. Cached per script; creates
    nothing."""
    return spamcheck.check_endpoint(body.subject, body.body)


@router.post("/spam-improve", response_model=SpamImproveOut)
def spam_improve(body: SpamImproveIn,
                 user: User = Depends(get_current_user),
                 tenant_id: str | None = Depends(campaign_tenant_id)) -> dict:
    """The one-click fix: the pitch rewritten without its spam triggers
    (AI best-effort, deterministic rules as the guaranteed fallback).
    Nothing is scheduled or sent — the result goes back to the user's
    editor for review."""
    return spamcheck.improve_endpoint(body.subject, body.body)


@router.get("", response_model=CampaignsOut)
def list_campaigns(
    user: User = Depends(get_current_user),
    tenant_id: str | None = Depends(campaign_tenant_id),
) -> dict:
    by_id = {a["id"]: a["email"]
             for a in get_email_store().list_for_user(user.id, tenant_id=tenant_id)}
    out = []
    for c in get_campaign_store().list_for_user(user.id, tenant_id=tenant_id):
        c["account_email"] = by_id.get(c["account_id"], "")
        c["account_emails"] = [by_id[a] for a in c["account_ids"]
                               if a in by_id]
        out.append(c)
    return {"campaigns": out}


@router.get("/track/{token}")
def track_open(token: str) -> Response:
    """The open-tracking pixel: an invisible 1x1 GIF named by an
    HMAC-signed send id. Deliberately UNAUTHENTICATED — an <img> tag
    carries no JWT; the signature in the token is the auth. A random or
    forged URL gets the same gif and marks nothing (no probing oracle).
    The store filters fires by time: the sender's own just-sent copy and
    re-fetches of the same view don't count (see tracking.py)."""
    if token.endswith(".png"):
        token = token[:-4]
    send_id = parse_token(token)
    if send_id is not None and not multi_tenant_enabled():
        get_campaign_store().mark_opened(
            send_id, opened_at=datetime.now(timezone.utc).isoformat())
    return Response(
        content=PIXEL_GIF, media_type="image/gif",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


def _campaign_detail(store, campaign_id: int, user: User,
                     by_id: dict[int, str],
                     tenant_id: str | None = None) -> dict | None:
    """The GET /campaigns/{id} payload, shared with PUT (edit returns the
    refreshed detail so the UI updates in one round-trip)."""
    c = store.get(campaign_id, user.id, tenant_id=tenant_id)
    if c is None:
        return None
    sends = store.sends(campaign_id, user.id, tenant_id=tenant_id) or []
    c["account_email"] = by_id.get(c["account_id"], "")
    c["account_emails"] = [by_id[a] for a in c["account_ids"] if a in by_id]
    c["sends"] = sends
    c["followups"] = store.followups(campaign_id, tenant_id=tenant_id)
    return c


@router.get("/{campaign_id}", response_model=CampaignDetailOut)
def get_campaign(campaign_id: int,
                 user: User = Depends(get_current_user),
                 tenant_id: str | None = Depends(campaign_tenant_id)) -> dict:
    by_id = {a["id"]: a["email"]
             for a in get_email_store().list_for_user(user.id, tenant_id=tenant_id)}
    c = _campaign_detail(get_campaign_store(), campaign_id, user, by_id, tenant_id)
    if c is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    return c


@router.put("/{campaign_id}", response_model=CampaignDetailOut)
def update_campaign(
    campaign_id: int, body: CampaignUpdateIn,
    user: User = Depends(get_current_user),
    tenant_id: str | None = Depends(campaign_tenant_id),
) -> dict:
    """Edit the pitch (name/subject/body) of a campaign that already
    started. Every send that has NOT gone out yet uses the new text;
    already-sent rows keep the subject they were actually sent with."""
    store = get_campaign_store()
    if store.get(campaign_id, user.id, tenant_id=tenant_id) is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    if not store.update_campaign(
            campaign_id, user.id, name=body.name.strip(),
            subject=body.subject, body=body.body, tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="no such campaign")
    logger.info("campaign %d pitch edited by %s", campaign_id, user.username)
    by_id = {a["id"]: a["email"]
             for a in get_email_store().list_for_user(user.id, tenant_id=tenant_id)}
    c = _campaign_detail(store, campaign_id, user, by_id, tenant_id)
    if c is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    return c


@router.post("/{campaign_id}/pause")
def pause_campaign(campaign_id: int,
                   user: User = Depends(get_current_user),
                   tenant_id: str | None = Depends(campaign_tenant_id)) -> dict:
    store = get_campaign_store()
    c = store.get(campaign_id, user.id, tenant_id=tenant_id)
    if c is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    if c["status"] not in ("running", "scheduled"):
        raise HTTPException(status_code=409,
                            detail=f"campaign is {c['status']}")
    store.set_status(campaign_id, status="paused", paused_reason="user",
                     tenant_id=tenant_id)
    logger.info("campaign %d paused by %s", campaign_id, user.username)
    return {"id": campaign_id, "status": "paused"}


@router.post("/{campaign_id}/resume")
def resume_campaign(campaign_id: int,
                    user: User = Depends(get_current_user),
                    tenant_id: str | None = Depends(campaign_tenant_id)) -> dict:
    store = get_campaign_store()
    c = store.get(campaign_id, user.id, tenant_id=tenant_id)
    if c is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    if c["status"] != "paused":
        raise HTTPException(status_code=409,
                            detail=f"campaign is {c['status']}")
    # Not started yet? It goes back to 'scheduled' (its start_at still
    # rules), never straight to running.
    now = datetime.now(timezone.utc)
    start = parse_ts(c["start_at"]) or (now - timedelta(seconds=1))
    if start > now:
        store.set_status(campaign_id, status="scheduled", tenant_id=tenant_id)
        return {"id": campaign_id, "status": "scheduled"}
    store.set_status(campaign_id, status="running", tenant_id=tenant_id)
    logger.info("campaign %d resumed by %s", campaign_id, user.username)
    return {"id": campaign_id, "status": "running"}


@router.delete("/{campaign_id}")
def delete_campaign(campaign_id: int,
                    user: User = Depends(get_current_user),
                    tenant_id: str | None = Depends(campaign_tenant_id)) -> dict:
    store = get_campaign_store()
    if not store.delete(campaign_id, user.id, tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="no such campaign")
    logger.info("campaign %d deleted by %s", campaign_id, user.username)
    return {"id": campaign_id, "deleted": True}
