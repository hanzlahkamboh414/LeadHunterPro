"""Phones API — the Phones vertical's endpoints (P3, calling workflow P7.5).

Category-gated: only accounts whose signup category includes "phones"
(phones | both) may call these; the admin is always allowed. The gate is a
FastAPI dependency mirroring require_admin (app.auth.dependencies).

Endpoints
---------
POST /phones/search              pure-SQL instant serve (trade-less since
                                 P7.5: state + quantity is the whole form)
GET  /phones/leads               today's still-owned call sheet (UTC)
GET  /phones/stats               eligible states; inventory counts admin-only
POST /phones/leads/{id}/lead     ✓Lead — person said "project doonga": snapshot
                                 to My Leads, retire the number for good
POST /phones/leads/{id}/voicemail ☃Voicemail — park 14/30/60 days, 4th retires
POST /phones/leads/{id}/store    💾Store — keep as a contact (stays claimed)
POST /phones/leads/{id}/note     📝Note — auto-stores a contact + the note
GET  /phones/saved               My Leads + My Contacts (kind filter)
PUT  /phones/saved/{id}/note     edit a saved note
DELETE /phones/saved/{id}        delete a saved lead/contact (user's own rows)
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.activity import get_activity
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.phones.service import phone_search
from app.phones.store import PhoneLeadsStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/phones", tags=["Phones"])

# Shared singleton — tests override with a tmp-db instance.
_store: PhoneLeadsStore = PhoneLeadsStore()

#: Simple (non-admin) accounts are capped per search. Phones are cheap to
#: harvest (plain HTTP, no AI) so the cap is higher than the email vertical's
#: 150 — but still bounded so one user cannot drain a whole state's pool.
MAX_USER_TARGET_PHONES = 1000


def require_phone_category(user: User = Depends(get_current_user)) -> User:
    """P3 category gate: the Phones vertical is for phone|both accounts.

    Admins are always allowed (the admin account predates categories and
    owns the platform).
    """
    if user.is_admin or user.category in ("phones", "both"):
        return user
    raise HTTPException(
        status_code=403,
        detail=(
            "This account is signed up for the Emails vertical only — "
            "phone leads are not included."
        ),
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PhoneSearchIn(BaseModel):
    #: Trade is OPTIONAL since P7.5 — phone users search state + quantity
    #: only (calling is about volume); '' serves the pool's mixed trades.
    trade: str = Field("", max_length=60)
    state: str = Field("", max_length=2)
    city: str = Field("", max_length=60)
    target: int = Field(25, ge=1, le=5000)


class NoteIn(BaseModel):
    note: str = Field("", max_length=2000)


class PhoneEventIn(BaseModel):
    action: str = Field(..., pattern="^(dialed|copied|not_interested|follow_up|no_answer|wrong_number)$")


class PhoneLeadOut(BaseModel):
    id: int
    phone: str
    person_name: str = ""
    business_name: str = ""
    trade: str = ""
    city: str = ""
    state: str = ""
    source: str = ""
    license_status: str = ""
    source_url: str = ""
    email: str = ""
    email_source: str = ""
    website: str = ""
    #: "pending" = the background enricher has not reached this lead yet;
    #: "found" = an email literally seen on the company's own site;
    #: "none" = enrichment ran, nothing findable (honest miss, never a guess).
    email_status: str = "pending"
    #: Calling workflow: how many voicemails this number has drawn so far
    #: (a resting row is off everyone's sheet until its cooldown passes).
    voicemail_count: int = 0

    class Config:
        from_attributes = True


class PhoneSavedOut(BaseModel):
    id: int
    phone: str
    person_name: str = ""
    business_name: str = ""
    trade: str = ""
    city: str = ""
    state: str = ""
    source: str = ""
    source_url: str = ""
    license_status: str = ""
    email: str = ""
    email_source: str = ""
    website: str = ""
    kind: str = "contact"
    note: str = ""
    created_at: str = ""
    updated_at: str = ""


class PhoneSearchOut(BaseModel):
    leads: list[PhoneLeadOut]
    served_from_pool: int
    fetched_live: int
    stocked_new: int
    banked_other_trade: int
    dropped_bad_phone: int
    coverage: list[str]
    reason: str = ""


def _lead_out(lead: dict[str, Any]) -> PhoneLeadOut:
    email = lead.get("email", "")
    enriched = bool(lead.get("enriched_at", ""))
    return PhoneLeadOut(
        id=int(lead["id"]),
        phone=lead.get("phone", ""),
        person_name=lead.get("person_name", ""),
        business_name=lead.get("business_name", ""),
        trade=lead.get("trade", ""),
        city=lead.get("city", ""),
        state=lead.get("state", ""),
        source=lead.get("source", ""),
        license_status=lead.get("license_status", ""),
        source_url=lead.get("source_url", ""),
        email=email,
        email_source=lead.get("email_source", ""),
        website=lead.get("website", ""),
        email_status="found" if email else ("none" if enriched else "pending"),
        voicemail_count=int(lead.get("voicemail_count") or 0),
    )


def _saved_out(row: dict[str, Any]) -> PhoneSavedOut:
    return PhoneSavedOut(
        id=int(row["id"]),
        phone=row.get("phone", ""),
        person_name=row.get("person_name", ""),
        business_name=row.get("business_name", ""),
        trade=row.get("trade", ""),
        city=row.get("city", ""),
        state=row.get("state", ""),
        source=row.get("source", ""),
        source_url=row.get("source_url", ""),
        license_status=row.get("license_status", ""),
        email=row.get("email", ""),
        email_source=row.get("email_source", ""),
        website=row.get("website", ""),
        kind=row.get("kind", "contact"),
        note=row.get("note", ""),
        created_at=row.get("created_at", ""),
        updated_at=row.get("updated_at", ""),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/search", response_model=PhoneSearchOut)
def search(
    body: PhoneSearchIn, user: User = Depends(require_phone_category),
) -> PhoneSearchOut:
    """Serve phone leads — instant pure-SQL serve from the harvester's pool."""
    if not user.is_admin and body.target > MAX_USER_TARGET_PHONES:
        raise HTTPException(
            status_code=422,
            detail=f"User accounts are limited to {MAX_USER_TARGET_PHONES} "
                   "phone leads per search (the admin account is unlimited).",
        )
    if not user.is_admin and body.target > _store.daily_remaining(user.id):
        raise HTTPException(
            status_code=422,
            detail=f"Only {_store.daily_remaining(user.id)} phone numbers remain "
                   "in today's allowance (UTC).",
        )
    outcome = phone_search(
        _store,
        trade=body.trade, state=body.state, city=body.city,
        target=body.target, user_id=user.id,
        enforce_quota=not user.is_admin,
    )
    logger.info(
        "POST /phones/search -> %d lead(s) for %s (trade=%s state=%s city=%r "
        "pool=%d live=%d stocked=%d)",
        len(outcome["leads"]), user.username, body.trade, body.state,
        body.city, outcome["served_from_pool"], outcome["fetched_live"],
        outcome["stocked_new"],
    )
    # P6 demand signal: what users search for is what the harvester stocks.
    # P7.5: a trade-less search records a STATE-level row (trade='') so the
    # harvester boosts every covered pair of that state. Telemetry only —
    # the hook is guarded so a harvester hiccup can never fail a search.
    try:
        from app.discovery.tradefold import normalize_trade
        from app.harvester.store import record_demand_safe
        record_demand_safe(
            normalize_trade(body.trade) or body.trade.strip().lower(),
            body.state,
        )
    except Exception:  # noqa: BLE001 — demand telemetry must never break a search
        logger.exception("harvester demand hook failed — search continues")
    get_activity().record(
        user.id, user.username, "phone_search",
        detail=f"{body.trade or 'all trades'} · {body.state or 'any'} · "
               f"{body.target} targets",
    )
    return PhoneSearchOut(
        leads=[_lead_out(l) for l in outcome["leads"]],
        served_from_pool=outcome["served_from_pool"],
        fetched_live=outcome["fetched_live"],
        stocked_new=outcome["stocked_new"],
        banked_other_trade=outcome["banked_other_trade"],
        dropped_bad_phone=outcome["dropped_bad_phone"],
        coverage=outcome["coverage"],
        reason=outcome["reason"],
    )


@router.get("/leads", response_model=list[PhoneLeadOut])
def list_leads(
    trade: str = Query("", max_length=60),
    state: str = Query("", max_length=2),
    city: str = Query("", max_length=60),
    limit: int = Query(1000, ge=1, le=1000),
    user: User = Depends(require_phone_category),
) -> list[PhoneLeadOut]:
    """Today's still-owned claims from all searched states (UTC).

    Yesterday's rows leave the active sheet at midnight UTC, but ownership
    and dated call history remain. The 1000-row response cap exceeds a normal
    user's 600/day allowance; admin accounts may exceed it.
    """
    from app.discovery.tradefold import normalize_trade

    leads = _store.list_owned(
        user.id,
        trade=normalize_trade(trade), state=state, city=city, limit=limit,
    )
    return [_lead_out(l) for l in leads]


@router.get("/stats")
def stats(
    target: int = Query(25, ge=1, le=5000),
    user: User = Depends(require_phone_category),
) -> dict:
    """Eligible states for users; full inventory counts only for admins."""
    servable = _store.servable_by_state()
    common = {
        "mine": len(_store.list_owned(user.id, limit=1000)),
        "eligible_states": sorted(
            state for state, count in servable.items() if count >= target
        ),
        "daily_limit": None if user.is_admin else _store.daily_limit(user.id),
        "daily_used": _store.daily_usage(user.id),
        "daily_remaining": None if user.is_admin else _store.daily_remaining(user.id),
    }
    if user.is_admin:
        return {
            **_store.pool_stats(), **common,
            "servable_by_state": servable,
            "servable_total": sum(servable.values()),
        }
    return common


@router.get("/activity")
def call_activity(
    day: str = Query("", alias="date", max_length=10),
    user: User = Depends(require_phone_category),
) -> dict:
    if day:
        try:
            date.fromisoformat(day)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from exc
    return _store.call_activity(user.id, day)


@router.get("/activity/days")
def call_days(user: User = Depends(require_phone_category)) -> list[str]:
    return _store.call_days(user.id)


@router.post("/leads/{lead_id}/event")
def record_call_event(
    lead_id: int, body: PhoneEventIn,
    user: User = Depends(require_phone_category),
) -> dict:
    if body.action == "wrong_number":
        result = _store.mark_wrong_number(lead_id, user.id)
    else:
        result = _store.record_call_event(lead_id, user.id, body.action)
    return _act(lead_id, user, body.action, result)


# ---------------------------------------------------------------------------
# Calling workflow (P7.5) — ✓Lead / ☎Voicemail / 💾Store / 📝Note
# ---------------------------------------------------------------------------

def _act(lead_id: int, user: User, action: str,
         result: dict[str, Any] | None) -> dict[str, Any]:
    """Shared tail for the four action endpoints: owner-check outcome ->
    404 when the lead isn't the caller's, honest action result + activity
    log entry otherwise."""
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="No such phone lead on your call sheet (another user's "
                   "or already released).",
        )
    get_activity().record(user.id, user.username, f"phone_{action}",
                          detail=f"lead {lead_id}")
    return result


@router.post("/leads/{lead_id}/lead")
def mark_lead(
    lead_id: int, user: User = Depends(require_phone_category),
) -> dict:
    """✓Lead: the person said "project doonga" — snapshot to My Leads,
    retire the number (never served to anyone else, never re-harvested)."""
    return _act(lead_id, user, "lead", _store.mark_lead(lead_id, user.id))


@router.post("/leads/{lead_id}/voicemail")
def mark_voicemail(
    lead_id: int, user: User = Depends(require_phone_category),
) -> dict:
    """☎Voicemail: park the number (14/30/60-day tiers) — it recirculates
    to the shared rotation after the cooldown; a 4th voicemail retires it."""
    return _act(
        lead_id, user, "voicemail", _store.mark_voicemail(lead_id, user.id),
    )


@router.post("/leads/{lead_id}/store")
def store_contact(
    lead_id: int, user: User = Depends(require_phone_category),
) -> dict:
    """💾Store: keep the contact in the account (row stays claimed — it is
    still on the caller's sheet, never served to anyone else)."""
    return _act(
        lead_id, user, "store", _store.store_contact(lead_id, user.id),
    )


@router.post("/leads/{lead_id}/note")
def note_lead(
    lead_id: int, body: NoteIn,
    user: User = Depends(require_phone_category),
) -> dict:
    """📝Note: save a note against the lead (auto-stored as a contact)."""
    return _act(
        lead_id, user, "note", _store.note_lead(lead_id, user.id, body.note),
    )


@router.get("/saved", response_model=list[PhoneSavedOut])
def list_saved(
    kind: str = Query("", pattern="^(lead|contact)?$"),
    limit: int = Query(1000, ge=1, le=5000),
    user: User = Depends(require_phone_category),
) -> list[PhoneSavedOut]:
    """My Leads + My Contacts — the caller's saved rows (✓Lead + 💾Store
    output, notes included). ``kind`` filters to 'lead' or 'contact'."""
    return [_saved_out(r) for r in _store.list_saved(user.id, kind=kind,
                                                     limit=limit)]


@router.put("/saved/{saved_id}/note")
def set_saved_note(
    saved_id: int, body: NoteIn,
    user: User = Depends(require_phone_category),
) -> dict:
    """Edit the note on a saved lead/contact row."""
    if not _store.set_saved_note(saved_id, user.id, body.note):
        raise HTTPException(404, "No such saved row on your account.")
    return {"ok": True}


@router.delete("/saved/{saved_id}")
def delete_saved(
    saved_id: int, user: User = Depends(require_phone_category),
) -> dict:
    """Delete a saved lead/contact (the user's own row only — the pool is
    untouched by this)."""
    if not _store.delete_saved(saved_id, user.id):
        raise HTTPException(404, "No such saved row on your account.")
    return {"ok": True}
