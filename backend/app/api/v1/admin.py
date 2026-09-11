"""Admin role API — the operational dashboard (read views) + the management
functions that define the admin role (API keys, deleted-lead audit, caches).

Security rules (CLAUDE.md §6 — honest, never leaky):
* API keys are managed through the :class:`RuntimeKeyStore` overlay — the UI
  never sees, and this module never echoes, a full secret: only ``configured``
  + a masked ``••••tail``.
* Every write is logged by key NAME only (never the value).
* ``require_api_key`` guards the whole router (M12 baseline).
"""

from fastapi import APIRouter, Depends, HTTPException, Query

import logging

from app.admin_read import (
    AdminReadRepository,
    purge_search_cache,
    search_cache_status,
)
from app.api.v1.leads import _job_source_by_email, _store, require_api_key
from app.auth.activity import get_activity
from app.auth import dependencies as auth_deps
from app.auth.dependencies import require_admin
from app.auth.models import User, UserStore
from app.core.runtime_keys import KNOWN_KEY_NAMES, RuntimeKeyStore
from app.lead_research.service import _email_hash
from app.schemas.admin import (
    AdminActivityOut,
    AdminActivityRow,
    AdminAssignIn,
    AdminCachePendingOut,
    AdminDashboardOut,
    AdminDeletedOut,
    AdminKeysOut,
    AdminLeadActionOut,
    AdminLeadScopeIn,
    AdminPasswordIn,
    AdminPurgeOut,
    AdminSearchCacheOut,
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
    """Emails the user deleted, when, and why (manual vs Junk sweep)."""
    return AdminDeletedOut(**_reader.deleted_log(limit=limit))


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

@router.get("/users", response_model=AdminUsersOut)
def users() -> AdminUsersOut:
    """Every account (no password data ever leaves the backend)."""
    all_users = _user_store().list_all()
    return AdminUsersOut(
        total=len(all_users),
        users=[AdminUserOut(**u.to_dict()) for u in all_users],
    )


@router.post("/users", response_model=AdminUserOut, status_code=201)
def create_user(body: AdminUserCreateIn) -> AdminUserOut:
    """Admin creates an account directly (username + email + password)."""
    try:
        user = _user_store().create(
            username=body.username, email=body.email, password=body.password
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    _user_store().delete(user_id)
    logger.info("DELETE /admin/users/%s -> deleted %s", user_id, target.username)
    return {"success": True, "username": target.username}


@router.get("/activity", response_model=AdminActivityOut)
def activity(limit: int = Query(default=200, ge=1, le=1000),
             user_id: str = Query(default="")) -> AdminActivityOut:
    """Recent user activity (login / logout / signup / search), newest first.

    ``user_id`` filters to one account's history — the per-user drill-down.
    """
    rows = get_activity().list(limit=limit, user_id=user_id)
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