"""Campaigns API (Phase E3) — create, watch, pause/resume, delete.

The scheduler thread does the sending; these endpoints are the control
surface. Every route is per-user isolated, and creation is honest about
what it excluded (leads already emailed by an earlier campaign are dropped
and reported, never silently re-mailed).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.campaigns.scheduler import parse_ts
from app.campaigns.store import get_campaign_store
from app.email_accounts.store import get_email_store
from app.schemas.campaigns import (
    CampaignCreateIn,
    CampaignCreateOut,
    CampaignDetailOut,
    CampaignOut,
    CampaignsOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaigns", tags=["Campaigns"])


def _account_email(email_store, account_id: int, user_id: str) -> str:
    for a in email_store.list_for_user(user_id):
        if a["id"] == account_id:
            return a["email"]
    return ""


def _validate_start_at(start_at: str) -> str:
    dt = parse_ts(start_at)
    if dt is None:
        raise HTTPException(status_code=422, detail="start_at must be ISO-8601")
    return dt.astimezone(timezone.utc).isoformat()


@router.post("", response_model=CampaignCreateOut)
def create_campaign(
    body: CampaignCreateIn, user: User = Depends(get_current_user)
) -> dict:
    """Create a scheduled campaign. Leads that were already emailed (any
    earlier campaign of this user) are excluded and the response says how
    many — never silently re-mailed."""
    email_store = get_email_store()
    account = next(
        (a for a in email_store.list_for_user(user.id)
         if a["id"] == body.account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail="no such connected account")
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
    already = store.already_sent_emails(user.id, body.emails)
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
    )
    campaign["account_email"] = account["email"]
    logger.info("POST /campaigns -> %s (%d leads, %d excluded as already-sent)",
                campaign["name"], len(emails), len(already))
    return {"campaign": campaign, "excluded": len(already)}


@router.get("", response_model=CampaignsOut)
def list_campaigns(user: User = Depends(get_current_user)) -> dict:
    email_store = get_email_store()
    out = []
    for c in get_campaign_store().list_for_user(user.id):
        c["account_email"] = _account_email(email_store, c["account_id"], user.id)
        out.append(c)
    return {"campaigns": out}


@router.get("/{campaign_id}", response_model=CampaignDetailOut)
def get_campaign(campaign_id: int,
                 user: User = Depends(get_current_user)) -> dict:
    store = get_campaign_store()
    c = store.get(campaign_id, user.id)
    if c is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    sends = store.sends(campaign_id, user.id) or []
    c["account_email"] = _account_email(get_email_store(), c["account_id"], user.id)
    c["sends"] = sends
    c["followups"] = store.followups(campaign_id)
    return c


@router.post("/{campaign_id}/pause")
def pause_campaign(campaign_id: int,
                   user: User = Depends(get_current_user)) -> dict:
    store = get_campaign_store()
    c = store.get(campaign_id, user.id)
    if c is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    if c["status"] not in ("running", "scheduled"):
        raise HTTPException(status_code=409,
                            detail=f"campaign is {c['status']}")
    store.set_status(campaign_id, status="paused", paused_reason="user")
    logger.info("campaign %d paused by %s", campaign_id, user.username)
    return {"id": campaign_id, "status": "paused"}


@router.post("/{campaign_id}/resume")
def resume_campaign(campaign_id: int,
                    user: User = Depends(get_current_user)) -> dict:
    store = get_campaign_store()
    c = store.get(campaign_id, user.id)
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
        store.set_status(campaign_id, status="scheduled")
        return {"id": campaign_id, "status": "scheduled"}
    store.set_status(campaign_id, status="running")
    logger.info("campaign %d resumed by %s", campaign_id, user.username)
    return {"id": campaign_id, "status": "running"}


@router.delete("/{campaign_id}")
def delete_campaign(campaign_id: int,
                    user: User = Depends(get_current_user)) -> dict:
    store = get_campaign_store()
    if not store.delete(campaign_id, user.id):
        raise HTTPException(status_code=404, detail="no such campaign")
    logger.info("campaign %d deleted by %s", campaign_id, user.username)
    return {"id": campaign_id, "deleted": True}
