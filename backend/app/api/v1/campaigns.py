"""Campaigns API (Phase E3) — create, watch, pause/resume, delete.

The scheduler thread does the sending; these endpoints are the control
surface. Every route is per-user isolated, and creation is honest about
what it excluded (leads already emailed by an earlier campaign are dropped
and reported, never silently re-mailed).
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.auth.vertical_access import require_email_access
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.campaigns import spamcheck
from app.campaigns.compose import has_referral_request
from app.campaigns.scheduler import PAKISTAN_TZ, READONLY_SCOPE, ensure_access_token, parse_ts
from app.campaigns.store import get_campaign_store
from app.campaigns.templates import render, render_email_body, sample_context
from app.campaigns.tracking import PIXEL_GIF, parse_token
from app.email_accounts import google, smtp
from app.email_accounts.store import get_email_store
from app.email.bounce_learning import BounceStore
from app.schemas.campaigns import (
    CampaignCreateIn,
    CampaignCreateOut,
    CampaignDetailOut,
    CampaignOut,
    CampaignTestSendIn,
    CampaignTestSendOut,
    CampaignUpdateIn,
    CampaignsOut,
    FollowupIn,
    SpamCheckIn,
    SpamCheckOut,
    SpamImproveIn,
    SpamImproveOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaigns", tags=["Campaigns"])


def _sending_is_paused() -> bool:
    store = get_campaign_store()
    flag = os.path.join(os.path.dirname(store._db_path),
                        "campaign_sending_paused.flag")
    return os.path.exists(flag)


def _require_sending_available() -> None:
    """Keep creation and direct send/resume actions behind the global hold."""
    if _sending_is_paused():
        raise HTTPException(
            status_code=409,
            detail="Campaign sending is paused by the admin.",
        )


def _blocked_addresses(emails: list[str]) -> set[str]:
    store = BounceStore()
    try:
        campaign_store = get_campaign_store()
        invalid = campaign_store.invalid_checked_emails(emails)
        bounced = {str(email).lower() for email in emails
                   if store.lookup(str(email)) == "bounced"}
        return invalid | bounced
    finally:
        store.close()


def _available_lead_emails(
    user: User, *, count: int, folder: str = "*",
    recommendation: str = "contact_now",
) -> list[str]:
    """Pick unused, valid leads in discovery order across all folders or one."""
    from app.api.v1 import leads as leads_api
    from app.lead_research.scoring import regate_verdict

    if recommendation not in ("", "contact_now", "nurture"):
        raise HTTPException(422, "Unknown lead quality filter")
    status = get_campaign_store().recipient_statuses(user.id)
    used = set(status["queued"]) | set(status["emailed"]) | set(status["suppressed"])
    picked: list[str] = []
    offset = 0
    while len(picked) < count:
        page, total = leads_api._store.query_leads(
            **leads_api._view_scope(user), folder=folder or "*",
            recommendation=recommendation or None,
            limit=500, offset=offset,
        )
        candidates = [item["dossier"].email.strip().lower() for item in page]
        blocked = _blocked_addresses(candidates)
        for item in page:
            email = item["dossier"].email.strip().lower()
            verdict, _ = regate_verdict(item["dossier"])
            if verdict == "skip" or (recommendation and verdict != recommendation):
                continue
            if email and email not in used and email not in blocked:
                picked.append(email)
                used.add(email)
                if len(picked) == count:
                    break
        offset += len(page)
        if not page or offset >= total:
            break
    return picked


def _validate_start_at(start_at: str) -> str:
    dt = parse_ts(start_at)
    if dt is None:
        raise HTTPException(status_code=422, detail="start_at must be ISO-8601")
    return dt.astimezone(timezone.utc).isoformat()


def _validate_outreach_copy(body: str, followups: list[FollowupIn] | None) -> None:
    """Reject copy the send-time safety rule would otherwise block permanently."""
    if has_referral_request(body):
        raise HTTPException(
            status_code=422,
            detail="The campaign email asks for another contact. Address the recipient directly.",
        )
    for step, followup in enumerate(followups or [], 1):
        if has_referral_request(followup.body):
            raise HTTPException(
                status_code=422,
                detail=f"Follow-up {step} asks for another contact. Address the recipient directly.",
            )


@router.post("", response_model=CampaignCreateOut)
def create_campaign(
    body: CampaignCreateIn, user: User = Depends(require_email_access)
) -> dict:
    """Create a scheduled campaign. Exclude leads already queued or emailed
    by another campaign of this user, and report the excluded count."""
    _require_sending_available()
    email_store = get_email_store()
    owned = {a["id"]: a for a in email_store.list_for_user(user.id)}
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
    _validate_outreach_copy(body.body, body.followups)

    start_at = _validate_start_at(body.start_at)

    store = get_campaign_store()
    requested = (
        _available_lead_emails(
            user, count=body.audience_count,
            folder=body.audience_folder,
            recommendation=body.audience_recommendation,
        ) if body.audience_count is not None else body.emails
    )
    if body.audience_count is not None and len(requested) < body.audience_count:
        raise HTTPException(
            status_code=422,
            detail=f"Only {len(requested)} unused leads are available; reduce the quantity.",
        )
    already = store.already_sent_emails(user.id, requested)
    bounced = _blocked_addresses(requested)
    excluded = {str(e).lower() for e in already} | bounced
    emails = [e for e in requested if str(e).lower() not in excluded]
    if not emails:
        raise HTTPException(
            status_code=422,
            detail="No sendable leads remain: recipients were already queued, emailed, or invalid",
        )
    try:
        campaign = store.create(
            user.id, account_id=body.account_id, name=body.name.strip(),
            subject=body.subject, body=body.body, emails=emails,
            start_at=start_at, daily_limit=body.daily_limit,
            delay_min_s=body.delay_min_s, delay_max_s=body.delay_max_s,
            followups=[fu.model_dump() for fu in body.followups],
            account_ids=wanted[1:],
            ai_personalize=body.ai_personalize,
            ai_compose=body.ai_compose,
            ai_signature=body.ai_signature,
            recipient_limit=body.audience_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    campaign["account_email"] = owned[body.account_id]["email"]
    campaign["account_emails"] = [owned[a]["email"] for a in wanted]
    logger.info("POST /campaigns -> %s (%d leads, %d excluded as already-sent, "
                "%d accounts, ai_personalize=%s)",
                campaign["name"], len(emails), len(excluded), len(wanted),
                body.ai_personalize)
    return {"campaign": campaign, "excluded": len(excluded)}


@router.post("/test-send", response_model=CampaignTestSendOut)
def campaign_test_send(
    body: CampaignTestSendIn, user: User = Depends(require_email_access)
) -> dict:
    """Send the DRAFT pitch to your own address — the spam check. The drafted
    subject/body render with a sample lead, then go out immediately via the
    chosen account. Creates nothing: no campaign, no send row, no CRM event,
    and the recipient is never counted as already-emailed (so you can test as
    often as you like without polluting future campaigns)."""
    _require_sending_available()
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
    creds = email_store.get_credentials(body.account_id, user.id)
    if creds is None or (creds.get("provider") == "smtp" and not creds.get("smtp_password")) or (
        creds.get("provider") != "smtp" and not creds["access_token"]
        and not creds["refresh_token"]
    ):
        # Undecryptable tokens (key rotated) — honest reset, not a fake send.
        raise HTTPException(status_code=409,
                            detail="account credentials unreadable — reconnect the account")

    ctx = sample_context()
    subject = render(body.subject, ctx)
    email_body = render_email_body(body.body, ctx)

    access_token = ""
    if creds.get("provider") != "smtp":
        access_token = ensure_access_token(
            email_store, account_id=body.account_id, user_id=user.id,
            creds=creds, now=datetime.now(timezone.utc))
        if not access_token:
            email_store.mark_status(body.account_id, user.id, "revoked")
            raise HTTPException(status_code=409,
                                detail="token refresh failed — reconnect the account")

    try:
        if creds.get("provider") == "smtp":
            smtp.send_smtp(
                host=creds["smtp_host"], port=creds["smtp_port"],
                security=creds["smtp_security"], username=creds["smtp_username"],
                password=creds["smtp_password"], from_email=creds["email"],
                to=str(body.to_email), subject=subject, body=email_body,
            )
        else:
            google.send_gmail(
                access_token, to=body.to_email, subject=subject, body=email_body,
                from_email=creds["email"],
            )
    except smtplib.SMTPAuthenticationError as exc:
        email_store.mark_status(body.account_id, user.id, "revoked")
        raise HTTPException(status_code=409, detail="SMTP login failed; reconnect this account") from exc
    except Exception as exc:  # noqa: BLE001 — Gmail's error shape varies
        if creds.get("provider") == "smtp":
            logger.warning("Campaign SMTP test send failed for account %d", body.account_id)
            raise HTTPException(status_code=502, detail="SMTP test send failed") from exc
        logger.warning("Campaign test send via %s failed: %s",
                       creds["email"], exc)
        email_store.mark_status(body.account_id, user.id, "revoked")
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
    email_store.mark_status(body.account_id, user.id, "connected")
    logger.info("POST /campaigns/test-send -> %s via %s",
                body.to_email, creds["email"])
    return {"sent": True, "to": body.to_email, "from_email": creds["email"],
            "subject": subject}


@router.post("/spam-check", response_model=SpamCheckOut)
def spam_check(body: SpamCheckIn,
               user: User = Depends(require_email_access)) -> dict:
    """How spammy does this pitch look? The AI reads the rendered email as
    a deliverability expert (score, plain-words summary, findings with
    fixes) and the rules engine always runs underneath — a blended
    verdict, rules-only when the AI is down. Cached per script; creates
    nothing."""
    return spamcheck.check_endpoint(body.subject, body.body)


@router.post("/spam-improve", response_model=SpamImproveOut)
def spam_improve(body: SpamImproveIn,
                 user: User = Depends(require_email_access)) -> dict:
    """The one-click fix: the pitch rewritten without its spam triggers
    (AI best-effort, deterministic rules as the guaranteed fallback).
    Nothing is scheduled or sent — the result goes back to the user's
    editor for review."""
    return spamcheck.improve_endpoint(body.subject, body.body)


@router.get("", response_model=CampaignsOut)
def list_campaigns(user: User = Depends(require_email_access)) -> dict:
    by_id = {a["id"]: a["email"]
             for a in get_email_store().list_for_user(user.id)}
    out = []
    for c in get_campaign_store().list_for_user(user.id):
        c["account_email"] = by_id.get(c["account_id"], "")
        c["account_emails"] = [by_id[a] for a in c["account_ids"]
                               if a in by_id]
        out.append(c)
    return {"campaigns": out}


@router.get("/sending-status")
def campaign_sending_status(
    user: User = Depends(require_email_access),
) -> dict[str, bool | str]:
    paused = _sending_is_paused()
    return {"paused": paused,
            "reason": "Paused by admin" if paused else ""}


@router.get("/recipient-status")
def recipient_status(user: User = Depends(get_current_user)) -> dict[str, list[str]]:
    """Let the lead picker distinguish unused, queued, and emailed addresses."""
    return get_campaign_store().recipient_statuses(user.id)


@router.get("/available-leads")
def available_leads(
    count: int = Query(50, ge=1, le=500),
    folder: str = Query("*", max_length=200),
    recommendation: str = Query("contact_now", max_length=40),
    user: User = Depends(require_email_access),
) -> dict:
    emails = _available_lead_emails(
        user, count=count, folder=folder, recommendation=recommendation,
    )
    return {"emails": emails, "count": len(emails), "requested": count}


@router.get("/activity")
def campaign_activity(
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_email_access),
) -> dict:
    rows = get_campaign_store().activity_for_user(user.id, limit, offset)
    accounts = {account["id"]: account["email"]
                for account in get_email_store().list_for_user(user.id)}
    for row in rows:
        row["account_email"] = accounts.get(row["account_id"], "")
    return {"rows": rows}


@router.get("/activity/explore")
def explore_campaign_activity(
    view: str = Query("sent", pattern="^(sent|replied|bounced|followup)$"),
    from_date: str = Query("", max_length=10),
    to_date: str = Query("", max_length=10),
    account_id: int = Query(0, ge=0),
    campaign_id: int = Query(0, ge=0),
    email: str = Query("", max_length=254),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_email_access),
) -> dict:
    for value in (from_date, to_date):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise HTTPException(422, "Dates must be YYYY-MM-DD") from exc
    if from_date and to_date and from_date > to_date:
        raise HTTPException(422, "From date must be before To date")
    result = get_campaign_store().explore_activity(
        user.id, view=view, from_date=from_date, to_date=to_date,
        account_id=account_id, campaign_id=campaign_id, email=email,
        limit=limit, offset=offset,
    )
    accounts = {
        account["id"]: account["email"]
        for account in get_email_store().list_for_user(user.id)
    }
    for row in result["rows"]:
        row["account_email"] = accounts.get(row["account_id"], "")
    for row in result["by_account"]:
        row["account_email"] = accounts.get(row["account_id"], "")
    return result


@router.get("/activity/{send_id}/timeline")
def campaign_recipient_timeline(
    send_id: int,
    campaign_id: int = Query(0, ge=0),
    email: str = Query("", max_length=254),
    user: User = Depends(require_email_access),
) -> dict:
    result = get_campaign_store().recipient_history(
        send_id, user.id, campaign_id=campaign_id, email=email)
    if result is None:
        raise HTTPException(404, "No such email record")
    accounts = {
        account["id"]: account["email"]
        for account in get_email_store().list_for_user(user.id)
    }
    for row in result["sends"]:
        row["account_email"] = accounts.get(row["account_id"], "")
    return result


@router.get("/activity/{send_id}/reply")
def campaign_reply_content(
    send_id: int,
    user: User = Depends(require_email_access),
) -> dict:
    context = get_campaign_store().reply_context(send_id, user.id)
    if context is None:
        raise HTTPException(404, "No detected reply for this send")
    account_id = context["account_id"]
    creds = get_email_store().get_credentials(account_id, user.id)
    if creds is None:
        raise HTTPException(404, "Sending account is no longer connected")
    if READONLY_SCOPE not in (creds.get("scopes") or ""):
        raise HTTPException(409, "Reconnect this Gmail in Settings to read replies")
    token = ensure_access_token(
        get_email_store(), account_id=account_id, user_id=user.id,
        creds=creds, now=datetime.now(timezone.utc),
    )
    if not token:
        raise HTTPException(409, "Reconnect this Gmail in Settings to read replies")
    sent_at = parse_ts(context["sent_at"]) or datetime.now(timezone.utc)
    after = int((sent_at - timedelta(hours=1)).timestamp())
    try:
        page = google.list_message_ids(
            token, label="", q=f"from:{context['email']} after:{after}", limit=50,
        )
        expected = context["reply_subject"].strip().lower()
        for message_id in page["ids"]:
            meta = google.get_message(token, message_id, metadata_only=True)
            if google.parse_from(meta["headers"].get("from", "")) != context["email"].lower():
                continue
            if expected and meta["headers"].get("subject", "").strip().lower() != expected:
                continue
            message = google.get_message(token, message_id)
            return {"email": context["email"],
                    "subject": message["headers"].get("subject", ""),
                    "date": message["headers"].get("date", ""),
                    "text": message.get("text") or message.get("snippet") or "",
                    "account_email": creds["email"], "found": True}
    except Exception as exc:
        logger.warning("Reply lookup failed for send %s: %s", send_id, exc)
        raise HTTPException(502, "Gmail could not load this reply right now") from exc
    return {"email": context["email"], "subject": context["reply_subject"],
            "date": context["replied_at"], "text": "",
            "account_email": creds["email"], "found": False}


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
    if send_id is not None:
        get_campaign_store().mark_opened(
            send_id, opened_at=datetime.now(timezone.utc).isoformat())
    return Response(
        content=PIXEL_GIF, media_type="image/gif",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


def _campaign_detail(store, campaign_id: int, user: User,
                     by_id: dict[int, str]) -> dict | None:
    """The GET /campaigns/{id} payload, shared with PUT (edit returns the
    refreshed detail so the UI updates in one round-trip)."""
    c = store.get(campaign_id, user.id)
    if c is None:
        return None
    sends = store.sends(campaign_id, user.id) or []
    c["account_email"] = by_id.get(c["account_id"], "")
    c["account_emails"] = [by_id[a] for a in c["account_ids"] if a in by_id]
    c["sends"] = sends
    c["followups"] = store.followups(campaign_id)
    return c


@router.get("/{campaign_id}", response_model=CampaignDetailOut)
def get_campaign(campaign_id: int,
                 user: User = Depends(require_email_access)) -> dict:
    by_id = {a["id"]: a["email"]
             for a in get_email_store().list_for_user(user.id)}
    c = _campaign_detail(get_campaign_store(), campaign_id, user, by_id)
    if c is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    return c


@router.get("/{campaign_id}/bounces")
def get_campaign_bounces(campaign_id: int,
                         user: User = Depends(require_email_access)) -> list[dict]:
    rows = get_campaign_store().bounces(campaign_id, user.id)
    if rows is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    return rows


@router.put("/{campaign_id}", response_model=CampaignDetailOut)
def update_campaign(
    campaign_id: int, body: CampaignUpdateIn,
    user: User = Depends(require_email_access),
) -> dict:
    """Change future campaign sends while preserving already-sent history."""
    store = get_campaign_store()
    current = store.get(campaign_id, user.id)
    if current is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    if current["status"] == "completed":
        raise HTTPException(status_code=409, detail="completed campaign cannot be edited")
    effective_min = body.delay_min_s if body.delay_min_s is not None else current["delay_min_s"]
    effective_max = body.delay_max_s if body.delay_max_s is not None else current["delay_max_s"]
    if effective_min > effective_max:
        raise HTTPException(status_code=422, detail="delay_min_s must be <= delay_max_s")
    _validate_outreach_copy(body.body, body.followups)
    new_start = _validate_start_at(body.start_at) if body.start_at is not None else None

    primary = body.account_id if body.account_id is not None else current["account_id"]
    extras = (body.account_ids if body.account_ids is not None else
              [aid for aid in current["account_ids"] if aid != primary])
    wanted = list(dict.fromkeys([primary, *(aid for aid in extras if aid != primary)]))
    if len(wanted) > 5:
        raise HTTPException(status_code=422, detail="at most 5 sending accounts per campaign")
    if body.account_id is not None or body.account_ids is not None:
        owned = {a["id"]: a for a in get_email_store().list_for_user(
            user.id)}
        for aid in wanted:
            account = owned.get(aid)
            if account is None:
                raise HTTPException(status_code=404, detail="no such connected account")
            if account["status"] != "connected" and aid not in current["account_ids"]:
                raise HTTPException(status_code=409,
                                    detail=f"account {account['email']} must be reconnected")
    safe_emails = None
    if body.emails is not None:
        bounced = _blocked_addresses(body.emails)
        safe_emails = [email for email in body.emails
                       if str(email).lower() not in bounced]
        if body.emails and not safe_emails:
            raise HTTPException(status_code=422, detail="All selected recipients are invalid")
    if not store.update_campaign(
            campaign_id, user.id, name=body.name.strip(),
            subject=body.subject, body=body.body,
            account_id=primary if body.account_id is not None or body.account_ids is not None else None,
            account_ids=wanted[1:] if body.account_id is not None or body.account_ids is not None else None,
            emails=safe_emails, start_at=new_start,
            daily_limit=body.daily_limit, delay_min_s=body.delay_min_s,
            delay_max_s=body.delay_max_s,
            followups=[fu.model_dump() for fu in body.followups]
            if body.followups is not None else None,
            ai_personalize=body.ai_personalize,
            ai_compose=body.ai_compose,
            ai_signature=body.ai_signature):
        raise HTTPException(status_code=409, detail="campaign is no longer editable")
    logger.info("campaign %d settings edited by %s", campaign_id, user.username)
    by_id = {a["id"]: a["email"]
             for a in get_email_store().list_for_user(user.id)}
    c = _campaign_detail(store, campaign_id, user, by_id)
    if c is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    return c


@router.post("/{campaign_id}/pause")
def pause_campaign(campaign_id: int,
                   user: User = Depends(require_email_access)) -> dict:
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
                    user: User = Depends(require_email_access)) -> dict:
    store = get_campaign_store()
    c = store.get(campaign_id, user.id)
    if c is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    if c["status"] != "paused":
        raise HTTPException(status_code=409,
                            detail=f"campaign is {c['status']}")
    _require_sending_available()
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


@router.post("/{campaign_id}/continue-now")
def continue_campaign_now(campaign_id: int,
                          user: User = Depends(require_email_access)) -> dict:
    """Start today's queue early without changing the campaign's daily clock."""
    store = get_campaign_store()
    c = store.get(campaign_id, user.id)
    if c is None:
        raise HTTPException(status_code=404, detail="no such campaign")
    _require_sending_available()
    if c["status"] == "paused" and c["paused_reason"] != "user":
        raise HTTPException(status_code=409,
                            detail="Reconnect the sending account or wait for its cooldown")
    if c["status"] not in ("scheduled", "running", "paused"):
        raise HTTPException(status_code=409,
                            detail="Campaign is complete; there are no emails to continue")
    now = datetime.now(timezone.utc)
    if store.next_pending(campaign_id, now.isoformat()) is None:
        next_at = parse_ts(store.next_pending_at(campaign_id))
        if next_at is not None:
            local_due = next_at.astimezone(PAKISTAN_TZ).strftime(
                "%d %b %Y at %I:%M %p PKT")
            detail = f"No email is due now. Next follow-up: {local_due}"
        else:
            detail = "No pending email is ready to send"
        raise HTTPException(status_code=409,
                            detail=detail)
    today = now.astimezone(PAKISTAN_TZ).date().isoformat()
    connected = {
        a["id"] for a in get_email_store().list_for_user(user.id)
        if a["status"] == "connected"
    }
    accounts = [a for a in c["account_ids"] if a in connected]
    if not accounts:
        raise HTTPException(status_code=409,
                            detail="Reconnect a sending account first")
    if all(store.sent_today_for_account(a, today) >= c["daily_limit"]
           for a in accounts):
        start = parse_ts(c["start_at"])
        start_clock = (start.astimezone(PAKISTAN_TZ).strftime("%I:%M %p")
                       if start is not None else "the set time")
        raise HTTPException(status_code=409,
                            detail=f"Today's daily limit is full. Sending can continue tomorrow after {start_clock} PKT")
    start = parse_ts(c["start_at"])
    already_active = (c["status"] == "running" and start is not None
                      and start <= now and now.astimezone(PAKISTAN_TZ).time()
                      >= start.astimezone(PAKISTAN_TZ).time())
    if already_active:
        return {"id": campaign_id, "status": "running",
                "early_resume_date": c["early_resume_date"],
                "message": "Campaign is already active. The next due email follows its configured delay."}
    if not store.continue_now(campaign_id, user.id, today):
        raise HTTPException(status_code=409,
                            detail="Campaign could not be continued")
    logger.info("campaign %d continued early by %s on %s PKT",
                campaign_id, user.username, today)
    return {"id": campaign_id, "status": "running",
            "early_resume_date": today,
            "message": "Campaign continued for today. The next due email follows its configured delay."}


@router.delete("/{campaign_id}")
def delete_campaign(campaign_id: int,
                    user: User = Depends(require_email_access)) -> dict:
    store = get_campaign_store()
    if not store.delete(campaign_id, user.id):
        raise HTTPException(status_code=404, detail="no such campaign")
    logger.info("campaign %d deleted by %s", campaign_id, user.username)
    return {"id": campaign_id, "deleted": True}
