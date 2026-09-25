"""Admin role API — the operational dashboard (read views) + the management
functions that define the admin role (API keys, deleted-lead audit, caches).

Security rules (CLAUDE.md §6 — honest, never leaky):
* API keys are managed through the :class:`RuntimeKeyStore` overlay — the UI
  never sees, and this module never echoes, a full secret: only ``configured``
  + a masked ``••••tail``.
* Every write is logged by key NAME only (never the value).
* ``require_api_key`` guards the whole router (M12 baseline).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

import logging
import sqlite3
from datetime import datetime, timezone

from app.admin_read import (
    AdminReadRepository,
    purge_search_cache,
    search_cache_status,
)
from app.api.v1.leads import _job_source_by_email, _store, require_api_key
from app.auth.activity import get_activity
from app.auth import dependencies as auth_deps
from app.auth.dependencies import multi_tenant_enabled, require_admin
from app.auth.models import User, UserStore
from app.core.runtime_keys import KNOWN_KEY_NAMES, RuntimeKeyStore
from app.lead_research.service import _email_hash
from app.phones.store import PhoneLeadsStore
from app.schemas.admin import (
    AdminActivityOut,
    AdminActivityRow,
    AdminAssignIn,
    AdminAuthModeIn,
    AdminCachePendingOut,
    AdminDashboardOut,
    AdminDecisionOut,
    AdminDeletedOut,
    AdminGmailInboxModeIn,
    AdminKeysOut,
    AdminLaneScheduleIn,
    AdminLaneStatusOut,
    AdminLeadActionOut,
    AdminLeadScopeIn,
    AdminPasswordIn,
    AdminPhoneClaimRow,
    AdminPhoneClaimsOut,
    AdminPurgeOut,
    AdminSearchCacheOut,
    AdminTenantCreateIn,
    AdminTenantMemberOut,
    AdminTenantOut,
    AdminUserCreateIn,
    AdminUserOut,
    AdminUsersOut,
    AdminUserSummaryOut,
    AdminVisibilityDateOut,
    AdminVisibilityOut,
    AdminKeyOut,
    KeyUpdateIn,
)

logger = logging.getLogger(__name__)


def _user_store() -> UserStore:
    """Resolve the auth store DYNAMICALLY via the module attribute — the same
    indirection ``get_current_user`` already has. A direct import binds the
    original function object at import time, which silently bypasses the
    (test-)patched store and would touch the real users.db."""
    return auth_deps._user_store()

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(require_api_key), Depends(require_admin)],
)

_reader = AdminReadRepository()
_key_store = RuntimeKeyStore()


def _keys_payload() -> AdminKeysOut:
    """Read the key status WITHOUT ever exposing a full secret."""
    from app.core.config import settings

    store = _key_store
    keys = []
    for name in KNOWN_KEY_NAMES:
        env_value = getattr(settings, name, "") or ""
        keys.append(
            AdminKeyOut(
                name=name,
                configured=store.is_set(name, env_value),
                masked=store.masked(name, env_value),
            )
        )
    return AdminKeysOut(
        keys=keys,
        provider=getattr(settings, "AI_PROVIDER", ""),
        base_url=getattr(settings, "AI_BASE_URL", ""),
        model=getattr(settings, "AI_MODEL", ""),
        searxng_url=getattr(settings, "SEARXNG_URL", ""),
        env=getattr(settings, "LEADHUNTER_ENV", ""),
        overlay_path=store.path,
        applies_after_restart=False,
    )


# ---------------------------------------------------------------------------
# Read views
# ---------------------------------------------------------------------------

@router.get("/dashboard", response_model=AdminDashboardOut)
def dashboard() -> AdminDashboardOut:
    """Operational metrics from the original lead database."""
    return _reader.dashboard()


@router.get("/keys", response_model=AdminKeysOut)
def keys() -> AdminKeysOut:
    """Which API keys are configured (masked) — for the admin screen.

    Full secrets never leave the backend (only ``••••tail``); ``base_url``,
    ``provider``, ``model`` and ``searxng_url`` are read-only config shown to
    orient the operator.
    """
    return _keys_payload()


@router.get("/deleted", response_model=AdminDeletedOut)
def deleted(limit: int = 100) -> AdminDeletedOut:
    """The user delete-feed: who deleted which email, when, why, and the
    admin's answer so far (pending / confirmed / restored)."""
    return AdminDeletedOut(**_reader.deleted_log(limit=limit))


@router.post("/deleted/{email}/confirm", response_model=AdminDecisionOut)
def deleted_confirm(email: str) -> AdminDecisionOut:
    """The admin's CONFIRM answer on a user delete.

    When the user said "not our client", the admin's confirm backs the verdict
    — the company+domain rejection becomes corroborated (an admin verdict is
    decisive: the identity is purged for everyone). The decision is recorded
    on the delete-feed row so the answer is visible.
    """
    result = _store.confirm_deleted(email)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no deleted lead for {email}")
    _log("delete-confirm", "email", email, 1)
    return AdminDecisionOut(**result)


@router.post("/deleted/{email}/restore", response_model=AdminDecisionOut)
def deleted_restore(email: str) -> AdminDecisionOut:
    """The admin's RESTORE answer: bring a wrongly-deleted lead back.

    Re-saves the stashed dossier (assigned back to the user who deleted it),
    lifts the delete-suppression (the email may resurface in future runs),
    and CLEARS the identity learning the delete had fed — the admin's word
    that the verdict was wrong reopens the company/domain for everyone.
    """
    result = _store.restore_deleted(email)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no deleted lead for {email}")
    if result.get("restored_dossier") == "yes":
        from app.lead_research.service import PendingLeadsStore

        PendingLeadsStore(db_path=_store._db_path).remove([email])
    _log("delete-restore", "email", email, 1)
    return AdminDecisionOut(**result)


@router.get("/cache/pending", response_model=AdminCachePendingOut)
def cache_pending(limit: int = 100) -> AdminCachePendingOut:
    """The discovery cache (pending_leads) — emails waiting to be researched."""
    return AdminCachePendingOut(**_reader.pending_emails(limit=limit))


@router.get("/cache/search", response_model=AdminSearchCacheOut)
def cache_search() -> AdminSearchCacheOut:
    """Provider-neutral search-cache status (rows, TTL, recent cached queries)."""
    return AdminSearchCacheOut(**search_cache_status())


# ---------------------------------------------------------------------------
# Management actions
# ---------------------------------------------------------------------------

@router.put("/keys", response_model=AdminKeysOut)
def update_key(body: KeyUpdateIn) -> AdminKeysOut:
    """Set or clear one API key — persisted AND applied live (no restart).

    ``value=""`` clears the key (falls back to its .env value, which may be
    empty). The overlay never touches .env and never echoes a secret. The
    change is hot-applied on the spot: the live ``settings`` object is updated
    in place (AI clients and the leads API key read it per-use) and Tavily /
    Brave provider instances are REBUILT with the new key. Only searches
    already in flight on the old instance complete with the old key.
    """
    if body.name not in KNOWN_KEY_NAMES:
        raise HTTPException(status_code=422, detail=f"unknown key {body.name!r}")
    _key_store.set(body.name, body.value)
    # Name only — the value must never reach a log.
    if (body.value or "").strip():
        logger.info("PUT /admin/keys -> %s set (value never logged)", body.name)
    else:
        logger.info("PUT /admin/keys -> %s cleared", body.name)

    # HOT APPLY — merge overlay + .env fallback into the RUNNING settings
    # object so every module holding a reference sees the new value now.
    from app.core.config import _ENV_KEY_VALUES, settings

    changed = _key_store.apply_live(settings, _ENV_KEY_VALUES)
    if changed:
        logger.info("PUT /admin/keys -> live-applied to settings: %s", ", ".join(changed))

    # Search providers hold the key inside their instance — rebuild them.
    from app.search_providers import re_register_configured_providers

    actions = re_register_configured_providers()
    for action in actions:
        logger.info("PUT /admin/keys -> provider %s (new key, old instance replaced)", action)

    return _keys_payload()


@router.post("/cache/purge-search", response_model=AdminPurgeOut)
def purge_cache_search() -> AdminPurgeOut:
    """Purge TTL-expired entries from the search cache (never discovery data).

    The search cache is disposable infrastructure — a paid query re-fills it.
    Returns exactly how many entries were dropped (CLAUDE.md §6, never silent).
    """
    removed = purge_search_cache()
    logger.info("POST /admin/cache/purge-search -> removed %d", removed)
    return AdminPurgeOut(removed=removed)


# ---------------------------------------------------------------------------
# Dashboard data control — hide / show / delete user-facing leads.
# ---------------------------------------------------------------------------

def _scope_emails(scope: str, value: str) -> list[str]:
    """Resolve a hide/show/delete/assign scope to the exact email set it names.

    * ``date``    — every dossier researched on that ``YYYY-MM-DD`` (the user's
      "7-8 tareekh ka data").
    * ``source``  — every dossier from that query run label (``trade · location``),
      the same ``source`` the lead list shows per row.
    * ``folder``  — every dossier organized into that named folder (the admin's
      "50-email folder push it to a user" flow).
    * ``email``   — a single lead (returns ``[]`` when no such dossier exists).

    Uses the SAME store + source map as the user-facing views, so an admin action
    always names exactly what the user sees. An empty/bogus value is an honest
    empty list, not an error (CLAUDE.md §6).
    """
    if scope == "email":
        return [value] if _store.get(value) is not None else []
    if scope == "date":
        date_by_hash = _store.research_dates()
        return [
            d.email
            for d in _store.list_all()
            if date_by_hash.get(_email_hash(d.email), "") == value
        ]
    if scope == "folder":
        meta_by_hash = _store.all_meta()
        return [
            d.email
            for d in _store.list_all()
            if (m := meta_by_hash.get(_email_hash(d.email))) is not None
            and m.folder == value
        ]
    # source
    by_email = _job_source_by_email()
    return [email for email, src in by_email.items() if src == value]


@router.get("/leads/visibility", response_model=AdminVisibilityOut)
def leads_visibility() -> AdminVisibilityOut:
    """Per-date data-control view — the admin sees the live, unfiltered truth.

    Each date row carries its total (admin count) + hidden (how much of it the
    USER dashboard is hiding right now). This is what the admin picks from
    before Hide / Show / Delete.
    """
    by_date = _store.visibility_by_date()
    return AdminVisibilityOut(
        total=sum(r["total"] for r in by_date.values()),
        hidden=len(_store.hidden_hashes()),
        by_date=[
            AdminVisibilityDateOut(date=d, total=r["total"], hidden=r["hidden"])
            for d, r in sorted(by_date.items(), reverse=True)
        ],
    )


def _log(action: str, scope: str, value: str, affected: int) -> None:
    logger.info("POST /admin/leads/%s %s=%r -> %d", action, scope, value, affected)


@router.post("/leads/hide", response_model=AdminLeadActionOut)
def leads_hide(body: AdminLeadScopeIn) -> AdminLeadActionOut:
    """Hide a date/search/lead from the USER dashboard (reversible via show).

    The dossiers stay in the DB and in the admin's unfiltered views — only the
    user-facing lead list/dates/folder counts drop them. Never silent: returns
    exactly how many were hidden (§6).
    """
    emails = _scope_emails(body.scope, body.value)
    affected = _store.set_hidden_bulk(emails, True)
    _log("hide", body.scope, body.value, affected)
    return AdminLeadActionOut(affected=affected, emails=emails)


@router.post("/leads/show", response_model=AdminLeadActionOut)
def leads_show(body: AdminLeadScopeIn) -> AdminLeadActionOut:
    """Un-hide — bring a date/search/lead back to the user dashboard."""
    emails = _scope_emails(body.scope, body.value)
    affected = _store.set_hidden_bulk(emails, False)
    _log("show", body.scope, body.value, affected)
    return AdminLeadActionOut(affected=affected, emails=emails)


@router.post("/leads/delete", response_model=AdminLeadActionOut)
def leads_delete(body: AdminLeadScopeIn) -> AdminLeadActionOut:
    """Permanently DELETE a date/search/lead from the server (irreversible).

    Drops every dossier in scope (via the existing :meth:`LeadResearchStore.delete`,
    which writes the ``deleted_leads`` audit) AND the matching discovery-cache rows
    so a future search does not re-surface the dropped addresses (same discipline
    as ``DELETE /leads/{email}`` / ``clear-junk``). The disposable search cache and
    all git state are untouched. Returns exactly how many were removed (§6).
    """
    emails = _scope_emails(body.scope, body.value)
    removed = [e for e in emails if _store.delete(e, reason="admin")]
    if removed:
        from app.lead_research.service import PendingLeadsStore

        PendingLeadsStore(db_path=_store._db_path).remove(removed)
    _log("delete", body.scope, body.value, len(removed))
    return AdminLeadActionOut(affected=len(removed), emails=removed)


# ---------------------------------------------------------------------------
# User management — accounts + the activity log.
# ---------------------------------------------------------------------------

def _require_tenant_mode() -> None:
    if not multi_tenant_enabled():
        raise HTTPException(status_code=404, detail="Tenant mode is not enabled")


def _admin_phone_tenant(
    request: Request, admin: User = Depends(require_admin),
) -> str | None:
    """Select only a workspace the admin currently owns or administers."""
    if not multi_tenant_enabled():
        return None
    context = auth_deps.get_tenant_context(request, admin)
    if context.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Tenant admin access required")
    return context.tenant_id


@router.get("/tenants", response_model=list[AdminTenantOut])
def list_tenants() -> list[AdminTenantOut]:
    """Platform-admin tenant registry and current user counts."""
    _require_tenant_mode()
    return [AdminTenantOut(**row) for row in _user_store().list_tenants()]


@router.post("/tenants", response_model=AdminTenantOut, status_code=201)
def create_tenant(body: AdminTenantCreateIn, admin: User = Depends(require_admin)) -> AdminTenantOut:
    """Create a tenant with the platform admin as its first owner."""
    _require_tenant_mode()
    store = _user_store()
    try:
        tenant_id = store.create_tenant(body.name, owner_user_id=admin.id)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Tenant name already exists") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AdminTenantOut(id=tenant_id, name=body.name.strip(), member_count=1)


@router.get("/tenants/{tenant_id}/members", response_model=list[AdminTenantMemberOut])
def tenant_members(tenant_id: str) -> list[AdminTenantMemberOut]:
    _require_tenant_mode()
    members = _user_store().list_tenant_members(tenant_id)
    if members is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return [AdminTenantMemberOut(**member) for member in members]


@router.put("/tenants/{tenant_id}/members/{user_id}")
def add_tenant_member(tenant_id: str, user_id: str) -> dict:
    """Assign an existing account as a member, never silently change its role."""
    _require_tenant_mode()
    store = _user_store()
    existing_role = store.tenant_role(user_id, tenant_id)
    if existing_role not in (None, "member"):
        raise HTTPException(status_code=409, detail="Existing role cannot be changed here")
    try:
        store.grant_membership(tenant_id, user_id, "member")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"tenant_id": tenant_id, "user_id": user_id, "role": "member"}


@router.delete("/tenants/{tenant_id}/members/{user_id}")
def remove_tenant_member(tenant_id: str, user_id: str) -> dict:
    _require_tenant_mode()
    try:
        removed = _user_store().revoke_membership(tenant_id, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Tenant membership not found")
    return {"removed": True}

@router.get("/users", response_model=AdminUsersOut)
def users(tenant_id: str | None = Depends(_admin_phone_tenant)) -> AdminUsersOut:
    """Every account (no password data ever leaves the backend)."""
    all_users = _user_store().list_all()
    if tenant_id is not None:
        all_users = [
            user for user in all_users
            if _user_store().tenant_role(user.id, tenant_id) is not None
        ]
    from app.api.v1.phones import _store as phone_store
    return AdminUsersOut(
        total=len(all_users),
        users=[AdminUserOut(
            **u.to_dict(), phone_daily_limit=phone_store.daily_limit(u.id, tenant_id),
            phone_daily_used=phone_store.daily_usage(u.id, tenant_id),
        ) for u in all_users],
    )


@router.post("/users", response_model=AdminUserOut, status_code=201)
def create_user(body: AdminUserCreateIn) -> AdminUserOut:
    """Admin creates an account directly (username + email + password)."""
    if multi_tenant_enabled() and not (body.tenant_id or "").strip():
        raise HTTPException(status_code=422, detail="tenant_id is required")
    try:
        user = _user_store().create(
            username=body.username, email=body.email,
            password=body.password, name=body.name,
            tenant_id=body.tenant_id if multi_tenant_enabled() else None,
        )
    except ValueError as exc:
        status = 422 if str(exc) == "tenant does not exist" else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    logger.info("POST /admin/users -> created %s", user.username)
    get_activity().record(user.id, user.username, "signup", detail="admin-created")
    return AdminUserOut(**user.to_dict())


@router.post("/users/{user_id}/password")
def reset_user_password(user_id: str, body: AdminPasswordIn) -> dict:
    """Admin resets any account's password by id."""
    target = _user_store().get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"user {user_id} not found")
    _user_store().reset_password(target.username, body.new_password)
    logger.info("POST /admin/users/%s/password -> reset %s", user_id, target.username)
    return {"success": True, "username": target.username}


@router.delete("/users/{user_id}")
def delete_user(user_id: str, admin: User = Depends(require_admin)) -> dict:
    """Delete an account. Its dossiers/jobs STAY in the DB (admin-panel visible;
    the user_id no longer resolves, so nobody else ever sees them). The admin
    cannot delete their own account.
    """
    if user_id == admin.id:
        raise HTTPException(status_code=422, detail="You cannot delete your own account")
    target = _user_store().get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"user {user_id} not found")
    if target.username == "shared":
        raise HTTPException(
            status_code=422,
            detail="The shared account is the anonymous identity while login auth is "
                   "off — it cannot be deleted",
        )
    _user_store().delete(user_id)
    logger.info("DELETE /admin/users/%s -> deleted %s", user_id, target.username)
    return {"success": True, "username": target.username}


@router.post("/auth-mode")
def set_auth_mode(body: AdminAuthModeIn, admin: User = Depends(require_admin)) -> dict:
    """Turn the login page ON/OFF.

    OFF = open site: visitors land straight in the normal user UI (token-less
    requests run as the shared account) and the admin panel is reachable only
    through the secret URL + password gate. ON = the classic login wall.
    """
    from app.auth.settings import get_settings

    if multi_tenant_enabled() and not body.enabled:
        raise HTTPException(
            status_code=409, detail="Login cannot be disabled in multi-tenant mode"
        )

    get_settings().set_auth_enabled(body.enabled)
    detail = "login auth ON" if body.enabled else "login auth OFF (open site)"
    get_activity().record(admin.id, admin.username, "auth", detail=detail)
    logger.info("POST /admin/auth-mode -> %s (by %s)", detail, admin.username)
    return {"auth_enabled": body.enabled}


@router.post("/gmail-inbox-mode")
def set_gmail_inbox_mode(
    body: AdminGmailInboxModeIn, admin: User = Depends(require_admin)
) -> dict:
    """Turn the Gmail-like inbox interface (browse/read/send) ON/OFF.

    OFF = every /gmail endpoint except the address export and /gmail/mode
    answers an honest 503; the export keeps working (it is the production
    feature). The frontend reads /gmail/mode and hides the browsing UI.
    """
    from app.auth.settings import get_settings

    get_settings().set_gmail_inbox_enabled(body.enabled)
    detail = ("Gmail inbox interface ON" if body.enabled
              else "Gmail inbox interface OFF (address export only)")
    get_activity().record(admin.id, admin.username, "gmail", detail=detail)
    logger.info("POST /admin/gmail-inbox-mode -> %s (by %s)",
                detail, admin.username)
    return {"inbox_enabled": body.enabled}


# ---------------------------------------------------------------------------
# Harvester AI-lane schedule — which lane runs, and for how long.
#
# The schedule lives in output/harvester.db (app/harvester/lane_schedule.py)
# and the worker re-reads it at the top of EVERY pass, so a save here is
# live: it lands on the next pass (<= HARVESTER_INTERVAL_S, default 5 min)
# with no backend restart and no worker rebuild.
# ---------------------------------------------------------------------------

def _lane_status_payload() -> AdminLaneStatusOut:
    """The stored schedule + what the worker will actually do next pass."""
    from app.core.config import settings
    from app.harvester.lane_schedule import get_lane_store

    now = datetime.now(timezone.utc)
    decision = get_lane_store().resolve(now)
    spec = decision.spec
    return AdminLaneStatusOut(
        mode=spec.mode,
        repeat=spec.repeat,
        phone_min=spec.phone_min,
        email_min=spec.email_min,
        phone_first=spec.phone_first,
        started_at=spec.started_at,
        effective_mode=decision.mode,
        in_phone_slot=decision.in_phone_slot,
        cycle_s=decision.cycle_s,
        position_s=decision.position_s,
        seconds_to_switch=decision.seconds_to_switch,
        next_switch_at=(
            decision.next_switch_at.isoformat(timespec="seconds")
            if decision.next_switch_at else ""
        ),
        one_time_done=decision.one_time_done,
        harvester_enabled=bool(getattr(settings, "HARVESTER_ENABLED", True)),
        interval_s=float(getattr(settings, "HARVESTER_INTERVAL_S", 300.0)),
        notes=[
            "phones lane = free license-board SODA fetches: ZERO AI spend. "
            "emails lane = the AI research lane; that is where the budget goes.",
            "This steers the HARVESTER only — a user's live Execute search is "
            "never blocked by this setting.",
            "The hourly Source Scout runs on its own cron and is NOT covered "
            "by this schedule.",
        ],
    )


@router.get("/harvester/lane", response_model=AdminLaneStatusOut)
def harvester_lane() -> AdminLaneStatusOut:
    """The live AI-lane schedule: stored spec + the lane the next pass runs."""
    return _lane_status_payload()


@router.post("/harvester/lane", response_model=AdminLaneStatusOut)
def set_harvester_lane(
    body: AdminLaneScheduleIn, admin: User = Depends(require_admin)
) -> AdminLaneStatusOut:
    """Save the AI-lane schedule — persisted AND picked up on the next pass.

    ``mode``: both | phones | emails | auto. In ``auto`` the cycle is
    anchored at the moment of this save (so "10 min phones, then 40 min
    emails" starts now, not at midnight), and ``repeat`` decides whether it
    loops all day or runs exactly one cycle before returning to both.

    An invalid combination answers 422 with the validator's own message — a
    schedule that cannot run is never stored as a silent no-op.
    """
    from app.harvester.lane_schedule import LaneSpec, get_lane_store

    spec = LaneSpec(
        mode=body.mode,
        phone_min=body.phone_min,
        email_min=body.email_min,
        phone_first=body.phone_first,
        repeat=body.repeat,
    )
    try:
        saved = get_lane_store().save(spec, datetime.now(timezone.utc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    detail = (
        f"AI lane schedule = {saved.mode}"
        + (f" ({saved.phone_min:g} min phones / {saved.email_min:g} min emails, "
           f"{'phones' if saved.phone_first else 'emails'} first, "
           f"{'repeating all day' if saved.repeat == 'day' else 'one cycle only'})"
           if saved.mode == "auto" else "")
    )
    get_activity().record(admin.id, admin.username, "harvest-lane", detail=detail)
    logger.info("POST /admin/harvester/lane -> %s (by %s)", detail, admin.username)
    return _lane_status_payload()


@router.get("/activity", response_model=AdminActivityOut)
def activity(limit: int = Query(default=200, ge=1, le=1000),
             user_id: str = Query(default=""),
             tenant_id: str | None = Depends(_admin_phone_tenant)) -> AdminActivityOut:
    """Recent user activity (login / logout / signup / search), newest first.

    ``user_id`` filters to one account's history — the per-user drill-down.
    """
    rows = get_activity().list(
        limit=limit, user_id=user_id, tenant_id=tenant_id,
    )
    return AdminActivityOut(
        total=len(rows),
        activity=[AdminActivityRow(**r) for r in rows],
    )


# ---------------------------------------------------------------------------
# Data control — push a folder/date/search to a user's dashboard.
# ---------------------------------------------------------------------------

@router.post("/leads/assign", response_model=AdminLeadActionOut)
def leads_assign(body: AdminAssignIn) -> AdminLeadActionOut:
    """Assign a slice of leads to a user's dashboard (or pull it back).

    Setting ``user_id`` makes every dossier in scope appear on that user's
    dashboard (their own-data view); ``user_id=""`` takes the leads back to
    admin-only. The admin panel keeps seeing everything regardless. The admin's
    "50-email folder -> single click -> user dashboard" flow.
    """
    emails = _scope_emails(body.scope, body.value)
    affected = _store.set_user_bulk(emails, body.user_id)
    _log("assign" if body.user_id else "unassign", body.scope, body.value, affected)
    return AdminLeadActionOut(affected=affected, emails=emails)


@router.get("/users/{user_id}/leads-summary", response_model=AdminUserSummaryOut)
def user_leads_summary(user_id: str) -> AdminUserSummaryOut:
    """What that user's dashboard currently holds — folders, dates, counts."""
    target = _user_store().get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"user {user_id} not found")
    catalog = _store.folder_catalog(user_id=user_id, is_admin=False)
    dates = _store.distinct_dates(user_id=user_id, is_admin=False)
    return AdminUserSummaryOut(
        user_id=user_id,
        username=target.username,
        folders={f["name"]: f["count"] for f in catalog["folders"]},
        unfiled=catalog["unfiled"],
        total=catalog["total"],
        dates=dates,
    )


@router.get("/phones/claims-report", response_model=AdminPhoneClaimsOut)
def phone_claims_report(
    admin: User = Depends(require_admin),
    tenant_id: str | None = Depends(_admin_phone_tenant),
) -> AdminPhoneClaimsOut:
    """Phone claims report — per user visible vs hidden claims.

    The call sheet shows only each user's LATEST batch. Older batches are
    hidden from the sheet but their ownership lives on (the numbers still
    serve to nobody). This report is the admin's eyes on that hidden stock —
    the data the user manages at main deploy.
    """
    from app.api.v1.phones import _store as phone_store

    rows = phone_store.claim_visibility_by_user(tenant_id=tenant_id)
    umap = {}
    try:
        for u in _user_store().list_all():
            umap[u.id] = u.username
    except Exception:  # pragma: no cover — usernames are best-effort
        pass
    out_rows = [
        AdminPhoneClaimRow(
            user_id=r["user_id"],
            username=umap.get(r["user_id"], ""),
            total=r["total"],
            visible=r["visible"],
            hidden=r["hidden"],
            first_claimed=r["first_claimed"],
            last_claimed=r["last_claimed"],
        )
        for r in rows
    ]
    return AdminPhoneClaimsOut(
        total_claims=sum(r["total"] for r in rows),
        total_hidden=sum(r["hidden"] for r in rows),
        by_user=out_rows,
    )


class PhoneDailyLimitIn(BaseModel):
    daily_limit: int = Field(..., ge=0, le=5000)


@router.put("/users/{user_id}/phone-limit")
def set_user_phone_limit(
    user_id: str, body: PhoneDailyLimitIn,
    tenant_id: str | None = Depends(_admin_phone_tenant),
) -> dict:
    """Admin override of one account's daily phone-claim allowance."""
    target = _user_store().get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if tenant_id is not None and _user_store().tenant_role(user_id, tenant_id) is None:
        raise HTTPException(status_code=404, detail="User not in selected tenant")
    from app.api.v1.phones import _store as phone_store
    phone_store.set_daily_limit(user_id, body.daily_limit, tenant_id=tenant_id)
    logger.info("PUT /admin/users/%s/phone-limit -> %d", user_id, body.daily_limit)
    return {
        "user_id": user_id,
        "daily_limit": phone_store.daily_limit(user_id, tenant_id),
    }


@router.get("/phones/wrong")
def wrong_phone_archive(
    tenant_id: str | None = Depends(_admin_phone_tenant),
) -> list[dict]:
    """Admin view of suppressed wrong numbers, retained indefinitely."""
    from app.api.v1.phones import _store as phone_store
    return phone_store.list_wrong_archive(tenant_id=tenant_id)


@router.post("/phones/wrong/{archive_id}/recover")
def recover_wrong_phone(
    archive_id: int, tenant_id: str | None = Depends(_admin_phone_tenant),
) -> dict:
    from app.api.v1.phones import _store as phone_store
    if not phone_store.recover_wrong_number(archive_id, tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="Wrong-number archive row unavailable")
    logger.info("POST /admin/phones/wrong/%s/recover", archive_id)
    return {"recovered": True}
