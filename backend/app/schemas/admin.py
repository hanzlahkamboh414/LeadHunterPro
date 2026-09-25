"""Admin-only operational dashboard response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AdminJobSummary(BaseModel):
    id: str
    query: dict = Field(default_factory=dict)
    state: str
    error: str = ""
    created_at: str
    updated_at: str
    elapsed_s: float = 0.0


class AdminDashboardOut(BaseModel):
    generated_at: str
    database_path: str
    dossiers_total: int
    recommendations: dict[str, int] = Field(default_factory=dict)
    pending: dict[str, int] = Field(default_factory=dict)
    jobs: dict[str, int] = Field(default_factory=dict)
    risks: dict[str, int] = Field(default_factory=dict)
    recent_jobs: list[AdminJobSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Admin — API-key management (masked only; secrets are never echoed).
# ---------------------------------------------------------------------------

class AdminKeyOut(BaseModel):
    name: str
    configured: bool
    masked: str = ""  # "••••abcd" or "" — NEVER the full value


class AdminKeysOut(BaseModel):
    keys: list[AdminKeyOut] = Field(default_factory=list)
    provider: str = ""    # AI_PROVIDER (e.g. "router")
    base_url: str = ""    # AI_BASE_URL (not secret)
    model: str = ""       # AI_MODEL
    searxng_url: str = ""  # SEARXNG_URL (not secret)
    env: str = ""         # LEADHUNTER_ENV
    overlay_path: str = ""
    applies_after_restart: bool = True


class KeyUpdateIn(BaseModel):
    name: str
    value: str  # "" = clear


# ---------------------------------------------------------------------------
# Admin — data-management views (deleted log + discovery/search cache).
# ---------------------------------------------------------------------------

class AdminDeletedRow(BaseModel):
    email: str
    deleted_at: str
    reason: str
    user_id: str = ""
    username: str = ""
    # The admin's answer so far: '' pending / 'confirmed' / 'restored'.
    admin_decision: str = ""


class AdminDeletedOut(BaseModel):
    total: int
    deleted: list[AdminDeletedRow] = Field(default_factory=list)


class AdminDecisionOut(BaseModel):
    """The admin's confirm/restore answer on one deleted lead."""
    email: str
    admin_decision: str
    # 'yes' when a stashed dossier was re-saved (restore), 'no' when only the
    # suppression was lifted (pre-snapshot delete), '' for confirm.
    restored_dossier: str = ""


class AdminPendingRow(BaseModel):
    email: str
    location: str = ""
    dead: bool = False
    attempted_at: str = ""
    attempt_count: int = 0


class AdminCachePendingOut(BaseModel):
    total: int
    active: int
    dead: int
    rows: list[AdminPendingRow] = Field(default_factory=list)


class AdminSearchCacheOut(BaseModel):
    path: str
    search_rows: int
    extract_rows: int
    search_ttl_days: int
    extract_ttl_days: int
    hit_rate: float = 0.0
    top_queries: list[dict] = Field(default_factory=list)


class AdminPurgeOut(BaseModel):
    removed: int


# ---------------------------------------------------------------------------
# Admin — Dashboard data control (hide / show / delete user-facing leads).
# ---------------------------------------------------------------------------

class AdminLeadScopeIn(BaseModel):
    """Which slice of the user dashboard data to act on.

    ``scope`` decides what ``value`` names: a ``date`` (``YYYY-MM-DD``) matches
    every dossier researched that day; a ``source`` matches every dossier from
    that query run (e.g. ``General Contractors · Dallas TX``); an ``email`` is a
    single lead. The admin picks date/search/email, then drives one operation.
    """
    scope: Literal["date", "source", "email"]
    value: str


class AdminLeadActionOut(BaseModel):
    """Result of a hide / show / delete — an honest, never-silent count (§6).

    ``emails`` lists exactly which leads the action affected, so the admin can
    verify what moved without a second read.
    """
    affected: int
    emails: list[str] = Field(default_factory=list)


class AdminVisibilityDateOut(BaseModel):
    date: str
    total: int
    hidden: int


class AdminVisibilityOut(BaseModel):
    """The per-date data-control view the admin acts on.

    ``by_date`` is newest-first; ``hidden`` is how many dossiers are hidden from
    the user dashboard right now (reversible via Show), ``total`` how many rows
    exist at all (the admin's live, unfiltered count).
    """
    total: int
    hidden: int
    by_date: list[AdminVisibilityDateOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Admin — user management + activity log.
# ---------------------------------------------------------------------------

class AdminUserOut(BaseModel):
    """One account row — password data never leaves the backend."""
    id: str
    username: str
    email: str
    is_admin: bool
    created_at: str
    name: str = ""
    phone_daily_limit: int = 600
    phone_daily_used: int = 0


class AdminUsersOut(BaseModel):
    total: int
    users: list[AdminUserOut] = Field(default_factory=list)


class AdminUserCreateIn(BaseModel):
    """Admin-creates-an-account payload (no signup page needed)."""
    username: str = Field(..., min_length=3, max_length=30)
    email: str = Field(..., min_length=5, max_length=100)
    password: str = Field(..., min_length=4, max_length=100)
    name: str = Field("", max_length=100)
    tenant_id: str | None = Field(None, max_length=100)


class AdminTenantCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class AdminTenantOut(BaseModel):
    id: str
    name: str
    member_count: int


class AdminTenantMemberOut(BaseModel):
    user_id: str
    username: str
    role: str


class AdminPasswordIn(BaseModel):
    new_password: str = Field(..., min_length=4, max_length=100)


class AdminAuthModeIn(BaseModel):
    """Toggle the login page on/off (the open-site mode switch)."""
    enabled: bool


class AdminGmailInboxModeIn(BaseModel):
    """Toggle the Gmail-like browsing interface on/off. The address export
    always stays on — this only gates browse/read/send."""
    enabled: bool


class AdminActivityRow(BaseModel):
    id: int
    user_id: str
    username: str
    action: str
    detail: str = ""
    created_at: str


class AdminActivityOut(BaseModel):
    total: int
    activity: list[AdminActivityRow] = Field(default_factory=list)


class AdminAssignIn(BaseModel):
    """Push (or pull back) a slice of leads to/from a user's dashboard.

    Same ``scope``/``value`` semantics as :class:`AdminLeadScopeIn`; ``user_id``
    names the account whose dashboard the leads appear on (``""`` = take back
    to admin-only).
    """
    scope: Literal["date", "source", "email", "folder"]
    value: str
    user_id: str = ""


class AdminUserSummaryOut(BaseModel):
    """Per-user data drill-down for the admin panel Users tab."""
    user_id: str
    username: str
    folders: dict[str, int] = Field(default_factory=dict)
    unfiled: int = 0
    total: int = 0
    dates: list[str] = Field(default_factory=list)


class AdminPhoneClaimRow(BaseModel):
    """One user's phone claim summary: how many the sheet shows vs hidden."""
    user_id: str
    username: str = ""
    total: int = 0
    visible: int = 0
    hidden: int = 0
    first_claimed: str = ""
    last_claimed: str = ""


class AdminPhoneClaimsOut(BaseModel):
    """Phone claims report — per user visible vs hidden claims.

    Hidden claims are still owned (nobody gets the number) but the sheet
    replaced them when a newer search landed. The admin needs this view
    to manage data at main deploy.
    """
    total_claims: int = 0
    total_hidden: int = 0
    by_user: list[AdminPhoneClaimRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Admin — harvester AI-lane schedule (which lane runs, and for how long).
# ---------------------------------------------------------------------------

class AdminLaneScheduleIn(BaseModel):
    """The admin screen's save payload for the lane schedule.

    ``mode`` is one of both/phones/emails/auto (the vocabulary is owned by
    app/harvester/lane_schedule.py and validated there — a bad value answers
    an honest 422, never a silently-stored no-op schedule).
    """
    mode: str
    phone_min: float = 30.0
    email_min: float = 90.0
    phone_first: bool = True
    repeat: Literal["day", "once"] = "day"


class AdminLaneStatusOut(BaseModel):
    """What the harvester is actually doing right now, plus the stored
    schedule. ``effective_mode`` is the lane the NEXT pass will run —
    for an ``auto`` spec it is the slot the clock is currently inside
    (never ``auto`` itself), and it reads ``both`` once a one-time cycle
    has finished."""

    mode: str
    repeat: str
    phone_min: float
    email_min: float
    phone_first: bool
    started_at: str = ""

    effective_mode: str
    in_phone_slot: bool | None = None
    cycle_s: float = 0.0
    position_s: float = 0.0
    seconds_to_switch: float | None = None
    next_switch_at: str = ""
    one_time_done: bool = False

    # Environment context — the operator needs to know whether the schedule
    # is even reaching a running worker, and how soon a change lands.
    harvester_enabled: bool = True
    interval_s: float = 300.0
    #: Honest limits, stated in the payload so the UI never has to invent them.
    notes: list[str] = Field(default_factory=list)
