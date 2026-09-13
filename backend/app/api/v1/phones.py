"""Phones API — the Phones vertical's endpoints (P3).

Category-gated: only accounts whose signup category includes "phones"
(phones | both) may call these; the admin is always allowed. The gate is a
FastAPI dependency mirroring require_admin (app.auth.dependencies).

Endpoints
---------
POST /phones/search   pool-first serve + live SODA gap-fill (synchronous —
                      license-board fetches are plain HTTP, no AI at serve
                      time by design; P7's harvester makes this pure SQL)
GET  /phones/leads    the caller's own (claimed) phone leads
GET  /phones/stats    honest pool inventory (totals + per-trade/state)
"""

from __future__ import annotations

import logging
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
    trade: str = Field(..., min_length=1, max_length=60)
    state: str = Field("", max_length=2)
    city: str = Field("", max_length=60)
    target: int = Field(25, ge=1, le=5000)


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

    class Config:
        from_attributes = True


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
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/search", response_model=PhoneSearchOut)
def search(
    body: PhoneSearchIn, user: User = Depends(require_phone_category),
) -> PhoneSearchOut:
    """Serve phone leads for a trade/state/city — pool first, live gap-fill."""
    if not user.is_admin and body.target > MAX_USER_TARGET_PHONES:
        raise HTTPException(
            status_code=422,
            detail=f"User accounts are limited to {MAX_USER_TARGET_PHONES} "
                   "phone leads per search (the admin account is unlimited).",
        )
    outcome = phone_search(
        _store,
        trade=body.trade, state=body.state, city=body.city,
        target=body.target, user_id=user.id,
    )
    logger.info(
        "POST /phones/search -> %d lead(s) for %s (trade=%s state=%s city=%r "
        "pool=%d live=%d stocked=%d)",
        len(outcome["leads"]), user.username, body.trade, body.state,
        body.city, outcome["served_from_pool"], outcome["fetched_live"],
        outcome["stocked_new"],
    )
    get_activity().record(
        user.id, user.username, "phone_search",
        detail=f"{body.trade} · {body.state or 'any'} · {body.target} targets",
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
    limit: int = Query(200, ge=1, le=1000),
    user: User = Depends(require_phone_category),
) -> list[PhoneLeadOut]:
    """The caller's own phone leads (claimed at serve)."""
    from app.discovery.tradefold import normalize_trade

    leads = _store.list_owned(
        user.id,
        trade=normalize_trade(trade), state=state, city=city, limit=limit,
    )
    return [_lead_out(l) for l in leads]


@router.get("/stats")
def stats(user: User = Depends(require_phone_category)) -> dict:
    """Honest pool inventory — what the vertical holds, per trade and state."""
    pool = _store.pool_stats()
    return {
        **pool,
        "mine": len(_store.list_owned(user.id, limit=1000)),
    }
