"""LinkedIn API — the LinkedIn vertical's endpoints (P4).

Available to EVERY authenticated account: the vertical is an email-research
BYPRODUCT (no quota, no live fetch, no AI at serve) — an emails-only or
phones-only account still sees the person leads research has produced.

Endpoints
---------
POST /linkedin/search   serve from the byproduct pool (pure SQL, instant —
                        there is no live LinkedIn fetch by design)
GET  /linkedin/leads    the caller's own (claimed) LinkedIn leads
GET  /linkedin/stats    honest pool inventory
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.activity import get_activity
from app.auth.dependencies import (
    get_current_user, get_tenant_context, multi_tenant_enabled,
)
from app.auth.models import User
from app.discovery.tradefold import normalize_trade
from app.linkedin.store import LinkedInLeadsStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/linkedin", tags=["LinkedIn"])

# Shared singleton — tests override with a tmp-db instance.
_store: LinkedInLeadsStore = LinkedInLeadsStore()

#: Pure sanity bound (one serve is one SQL query — this is page size, not a
#: product quota; the vertical itself is unquotted by design).
MAX_TARGET = 5000


class LinkedInSearchIn(BaseModel):
    trade: str = Field("", max_length=60)
    state: str = Field("", max_length=2)
    city: str = Field("", max_length=60)
    target: int = Field(25, ge=1, le=MAX_TARGET)


class LinkedInLeadOut(BaseModel):
    id: int
    person_name: str = ""
    role: str = ""
    linkedin_url: str = ""
    company_name: str = ""
    domain: str = ""
    trade: str = ""
    city: str = ""
    state: str = ""
    source: str = ""
    source_email: str = ""


class LinkedInSearchOut(BaseModel):
    leads: list[LinkedInLeadOut]
    served_from_pool: int
    reason: str = ""


def _lead_out(lead: dict[str, Any]) -> LinkedInLeadOut:
    return LinkedInLeadOut(
        id=int(lead["id"]),
        person_name=lead.get("person_name", ""),
        role=lead.get("role", ""),
        linkedin_url=lead.get("linkedin_url", ""),
        company_name=lead.get("company_name", ""),
        domain=lead.get("domain", ""),
        trade=lead.get("trade", ""),
        city=lead.get("city", ""),
        state=lead.get("state", ""),
        source=lead.get("source", ""),
        source_email=lead.get("source_email", ""),
    )


def linkedin_tenant_id(
    request: Request, user: User = Depends(get_current_user),
) -> str | None:
    """Recheck current membership on every tenant-mode LinkedIn request."""
    if not multi_tenant_enabled():
        return None
    return get_tenant_context(request, user).tenant_id


@router.post("/search", response_model=LinkedInSearchOut)
def search(
    body: LinkedInSearchIn, user: User = Depends(get_current_user),
    tenant_id: str | None = Depends(linkedin_tenant_id),
) -> LinkedInSearchOut:
    """Serve LinkedIn person leads from the byproduct pool.

    Pool-only by design: the inventory grows as email research runs, and a
    shortfall is reported honestly (never a scraped-on-demand result — that
    would be a quota and a ToS problem this vertical deliberately avoids).
    """
    slug = normalize_trade(body.trade)
    leads = _store.serve(
        slug, body.state.strip().upper()[:2], body.city.strip(),
        body.target, user.id, tenant_id=tenant_id,
    )
    reason = ""
    if len(leads) < body.target:
        reason = (
            f"the LinkedIn pool held {len(leads)} matching lead(s) — it grows "
            "as email research completes, there is no live LinkedIn fetch"
        )
    logger.info(
        "POST /linkedin/search -> %d lead(s) for %s (trade=%s state=%s)",
        len(leads), user.username, body.trade, body.state,
    )
    get_activity().record(
        user.id, user.username, "linkedin_search",
        detail=f"{body.trade or 'any'} · {body.state or 'any'} · "
               f"{body.target} targets",
        tenant_id=tenant_id,
    )
    return LinkedInSearchOut(
        leads=[_lead_out(l) for l in leads],
        served_from_pool=len(leads),
        reason=reason,
    )


@router.get("/leads", response_model=list[LinkedInLeadOut])
def list_leads(
    trade: str = Query("", max_length=60),
    state: str = Query("", max_length=2),
    city: str = Query("", max_length=60),
    limit: int = Query(200, ge=1, le=1000),
    user: User = Depends(get_current_user),
    tenant_id: str | None = Depends(linkedin_tenant_id),
) -> list[LinkedInLeadOut]:
    """The caller's own LinkedIn leads (claimed at serve)."""
    leads = _store.list_owned(
        user.id, trade=normalize_trade(trade), state=state, city=city,
        limit=limit, tenant_id=tenant_id,
    )
    return [_lead_out(l) for l in leads]


@router.get("/stats")
def stats(
    user: User = Depends(get_current_user),
    tenant_id: str | None = Depends(linkedin_tenant_id),
) -> dict:
    """Honest pool inventory — what email research has produced so far."""
    pool = _store.pool_stats()
    return {**pool, "mine": len(_store.list_owned(
        user.id, limit=1000, tenant_id=tenant_id,
    ))}
