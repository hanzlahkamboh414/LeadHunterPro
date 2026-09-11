"""Leads API — the frontend's backbone (M10 + M11 telemetry).

Drives the whole lead pipeline (discovery -> AI research) as a background
*job*, then lets the frontend poll live progress and read the finished
qualified leads.

Endpoints
---------
POST   /leads/jobs          submit a query run (background)
GET    /leads/jobs          list jobs
GET    /leads/jobs/{id}     live progress + partial results
POST   /leads/jobs/{id}/cancel   graceful stop
GET    /leads               qualified leads (filters)
GET    /leads/{email}       full researched dossier + evidence

Jobs run on a daemon worker thread (pipeline is blocking); the frontend polls
``GET /leads/jobs/{id}`` for telemetry (poll-based, CLAUDE.md §6 honest logging).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response

from app.auth.activity import get_activity
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.core.config import settings
from app.lead_research.service import LeadResearchStore, _email_hash
from app.leads.export import export_csv
from app.leads.jobs import JobManager
from app.leads.models import Job, JobState
from app.leads.pipeline import ResearchQuery
from app.schemas.leads import (
    FolderCreate,
    FoldersOut,
    FolderOut,
    JobCreate,
    JobOut,
    JobSummary,
    LeadDetail,
    LeadSummary,
    OrganizeClearIn,
    OrganizeIn,
    OrganizeRenameIn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/leads", tags=["Leads"])

# Shared singletons. Tests override these with tmp-db instances.
_manager: JobManager = JobManager()
_store: LeadResearchStore = LeadResearchStore()

#: Simple (non-admin) accounts are capped at 150 targets per search — the plan
#: limit the founder set. The admin is unlimited.
MAX_USER_TARGET_EMAILS = 150


def _view_scope(user: User) -> dict[str, Any]:
    """The per-user data scope every user-facing leads endpoint passes to the
    store.

    Every user sees their OWN dossiers/jobs. The admin additionally sees the
    legacy pre-auth rows (``user_id=''``) — but NOT other users' searches: those
    belong in the admin panel (per-user drill-down), not on the admin's own
    dashboard. Store methods take these exact kwargs.
    """
    return {"user_id": user.id, "is_admin": False, "include_legacy": user.is_admin}


# ---------------------------------------------------------------------------
# Auth (M12 baseline) — optional API key.
# ---------------------------------------------------------------------------

def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Reject when an API key is configured but not supplied."""
    if settings.LEADS_API_KEY and x_api_key != settings.LEADS_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _live_elapsed(job: Job) -> float:
    """Honest elapsed time for an IN-FLIGHT job.

    ``Job.elapsed_s`` is only set when the job COMPLETES (jobs.py), so a live
    job always reports ``0.0`` — the frontend then shows "0s elapsed" and its
    ETA estimate never engages. For queued/running/paused jobs we compute live
    elapsed from ``created_at`` (stored UTC, naive) against now. Completed /
    failed / cancelled jobs keep their stored (final) value.
    """
    if job.elapsed_s:
        return job.elapsed_s
    if job.state not in (JobState.queued, JobState.running, JobState.paused):
        return 0.0
    try:
        start = datetime.fromisoformat(job.created_at)
    except (ValueError, TypeError):
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - start).total_seconds())


def _job_out(job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        query=job.query,
        state=job.state.value,
        events=[e.to_dict() for e in job.events],
        results=job.results,
        pass_log=job.pass_log,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        elapsed_s=_live_elapsed(job),
        working_leads=job.working_leads,
        leads_found=job.leads_found,
        shortfall=job.shortfall,
        shortfall_reason=job.shortfall_reason,
    )


def _job_summary(job: Job) -> JobSummary:
    return JobSummary(
        id=job.id,
        query=job.query,
        state=job.state.value,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        elapsed_s=_live_elapsed(job),
        working_leads=job.working_leads,
        leads_found=job.leads_found,
        shortfall=job.shortfall,
        shortfall_reason=job.shortfall_reason,
    )


def _lead_summary(
    d: Any,
    *,
    recommendation: str | None = None,
    source: str = "",
    folder: str = "",
    tags: list[str] | None = None,
    created_at: str = "",
) -> LeadSummary:
    """One lead row.

    ``recommendation`` defaults to the CURRENT deterministic verdict — found via
    :func:`regate_recommendation` (a stored dossier's AI-era recommendation is
    never served stale). ``source`` names the query run that produced the lead
    (e.g. ``General Contractors · Dallas TX``), so the user can tell which
    search each row belongs to instead of an undifferentiated mix. ``folder``/
    ``tags`` are the user's organization metadata (Phase B); ``created_at`` is
    the extraction date (the lead's honest research date, ``YYYY-MM-DD``).
    """
    return LeadSummary(
        email=d.email,
        domain=d.domain,
        company=d.company.name,
        person=d.person.name,
        role=d.person.role,
        bound=d.person.bound,
        linkedin=d.person.linkedin,
        phone=d.person.phone,
        score=d.potential_score,
        recommendation=recommendation or d.recommendation,
        intent=d.intent.needs_estimation if d.intent else "",
        reason=d.intent.reason if d.intent else "",
        timing=d.timing.window if d.timing else "",
        source=source,
        folder=folder,
        tags=tags or [],
        created_at=created_at,
    )


def _user_owns_dossier(email: str, user: User) -> bool:
    """True when the user may access this dossier.

    Admin may touch any dossier. A non-admin may only touch a dossier whose
    ``user_id`` matches their own — never another user's, and never a legacy
    pre-auth row (those belong to the admin alone).
    """
    if user.is_admin:
        return True
    if not user.id:
        return False
    from app.lead_research.service import _email_hash

    conn = _store._conn()
    row = conn.execute(
        "SELECT user_id FROM dossiers WHERE email_hash = ?", (_email_hash(email),)
    ).fetchone()
    conn.close()
    return row is not None and row[0] == user.id


def _job_source_by_email(user_id: str = "", is_admin: bool = False,
                         include_legacy: bool = False) -> dict[str, str]:
    """Map email -> the query run that produced it, from persisted job results.

    Lets the leads list name each row's source run (``trade · location``) so a
    fresh search's leads are visibly separated from older runs — no more mixed,
    unlabeled data.

    When ``user_id`` is provided and the user is not admin, only the caller's
    own jobs are scanned — so source labels respect data isolation
    (``include_legacy`` adds the admin's pre-auth jobs).
    """
    out: dict[str, str] = {}
    uid = user_id if user_id and not is_admin else None
    for job in _manager.list_jobs(
        user_id=uid, include_legacy=include_legacy and uid is not None
    ):
        q = job.query or {}
        label = " · ".join(
            [str(q.get("trade", "")).strip(), str(q.get("location", "")).strip()]
        ).strip(" ·")
        if not label:
            label = job.id
        for r in job.results or []:
            em = r.get("email", "")
            if em and em not in out:
                out[em] = label
    return out


# ---------------------------------------------------------------------------
# Job endpoints
# ---------------------------------------------------------------------------

@router.post("/jobs", response_model=JobOut, status_code=201, dependencies=[Depends(require_api_key)])
def create_job(body: JobCreate, user: User = Depends(get_current_user)) -> JobOut:
    """Submit a query run; it executes in the background."""
    if not user.is_admin and body.target_emails > MAX_USER_TARGET_EMAILS:
        raise HTTPException(
            status_code=422,
            detail=f"User accounts are limited to {MAX_USER_TARGET_EMAILS} "
                   "targets per search (the admin account is unlimited).",
        )
    query = ResearchQuery(
        trade=body.trade,
        location=body.location,
        target_emails=body.target_emails,
        discover_only=body.discover_only,
        search_name=body.search_name,
        folder=body.folder,
    )
    try:
        query.validate()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    job = _manager.submit(query, user_id=user.id)
    logger.info("POST /leads/jobs -> %s (%s)", job.id, query.describe())
    get_activity().record(
        user.id, user.username, "search",
        detail=f"{query.trade} · {query.location} · {query.target_emails} targets",
    )
    return _job_out(job)


@router.get("/jobs", response_model=list[JobSummary], dependencies=[Depends(require_api_key)])
def list_jobs(user: User = Depends(get_current_user)) -> list[JobSummary]:
    scope = _view_scope(user)
    return [
        _job_summary(j)
        for j in _manager.list_jobs(
            user_id=scope["user_id"], include_legacy=scope["include_legacy"]
        )
    ]


@router.get("/jobs/{job_id}", response_model=JobOut, dependencies=[Depends(require_api_key)])
def get_job(job_id: str) -> JobOut:
    job = _manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return _job_out(job)


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_api_key)])
def cancel_job(job_id: str) -> dict[str, Any]:
    ok = _manager.cancel(job_id)
    if not ok:
        job = _manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
        raise HTTPException(status_code=409, detail=f"job {job_id} is {job.state.value}; cannot cancel")
    return {"job_id": job_id, "state": "cancelling"}


@router.post("/jobs/{job_id}/pause", dependencies=[Depends(require_api_key)])
def pause_job(job_id: str) -> dict[str, Any]:
    """Pause a running job; the worker blocks until resumed or cancelled."""
    ok = _manager.pause(job_id)
    if not ok:
        job = _manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
        raise HTTPException(status_code=409, detail=f"job {job_id} is {job.state.value}; cannot pause")
    return {"job_id": job_id, "state": "paused"}


@router.post("/jobs/{job_id}/resume", dependencies=[Depends(require_api_key)])
def resume_job(job_id: str) -> dict[str, Any]:
    """Resume a paused job."""
    ok = _manager.resume(job_id)
    if not ok:
        job = _manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
        raise HTTPException(status_code=409, detail=f"job {job_id} is {job.state.value}; cannot resume")
    return {"job_id": job_id, "state": "running"}


#: Discovery order — leads are listed number-wise, in the order they were
#: found (rowid ASC). No re-ranking on refresh: what the user saw stays put.
@router.get("", response_model=list[LeadSummary], dependencies=[Depends(require_api_key)])
def list_leads(
    recommendation: str | None = Query(default=None, description="filter by recommendation (contact_now/nurture/skip)"),
    bound: bool | None = Query(default=None, description="only leads with a bound decision-maker"),
    min_score: float | None = Query(default=None, ge=0, le=10, description="minimum potential score"),
    source: str | None = Query(default=None, description="only leads from this query run (e.g. 'General Contractors · Dallas TX')"),
    folder: str | None = Query(default=None, description="only leads in this folder"),
    tag: str | None = Query(default=None, description="only leads carrying this tag"),
    date: str | None = Query(default=None, description="only leads extracted on this date (YYYY-MM-DD)"),
    q: str | None = Query(default=None, description="identity text search (company/email/person/role)"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    response: Response = None,
    user: User = Depends(get_current_user),
) -> list[LeadSummary]:
    """List leads in DISCOVERY ORDER (number-wise) with honest defaults.

    ORDER — ``rowid ASC`` from the store: the order leads were found, stable
    across refresh. Lead #1 stays lead #1 after a refresh; the old tier/score
    re-sort moved rows on every refresh (the "leads jump to the bottom"
    complaint).

    DEFAULTS — ``skip`` is HIDDEN unless ``recommendation=skip`` is explicit:
    a dead-domain / junk dossier is not a lead, and hiding it by default keeps
    the totals honest (dead-domain leads no longer inflate the count).

    PLACES (mailbox model) — the Companies screen is an INBOX: the default
    (no ``folder``, no ``date``) shows only UNFILED leads. A lead you moved
    into a folder LEAVES this view and lives inside that folder — click the
    folder to see it (``?folder=NAME``). ``folder=*`` is "All" (every place,
    folders + inbox — the Dashboard's overview links). ``date`` is the one
    GLOBAL recall tool: with no folder selected it searches every dossier
    regardless of folder ("kis tareekh ko kya nikla").

    Each row's ``recommendation`` is the CURRENT deterministic verdict (old
    AI-era dossiers are re-gated, never served stale), and ``source`` names the
    query run that produced it — so a fresh search's leads are separated from
    older runs instead of mixing.

    ``q`` is an identity text search (company / refined company / email /
    person / role), matched as a substring — the exact filter the old client
    did in the browser, now answered server-side.

    FILTERING/PAGING IN THE DATABASE (Phase 2) — the old endpoint materialized
    EVERY dossier (full JSON) + re-gated + paginated last, so a request scanned
    the whole store. Now :meth:`LeadResearchStore.query_leads` filters, sorts and
    paginates in SQL on persisted verdict columns. Only the PAGE is returned; the
    honest count for the SAME filters rides in the ``X-Total-Count`` header so
    the frontend can page without ever downloading the store. The returned page
    is RE-GATED as a freshness backstop (a verdict that drifted since the row was
    written heals its column so the NEXT request filters it correctly).
    """
    from app.lead_research.scoring import regate_recommendation

    scope = _view_scope(user)
    source_by_email = _job_source_by_email(
        user_id=scope["user_id"],
        is_admin=scope["is_admin"],
        include_legacy=scope["include_legacy"],
    )
    source_emails = (
        {e for e, s in source_by_email.items() if s == source} if source else None
    )
    page, total = _store.query_leads(
        **scope,
        recommendation=recommendation,
        bound=bound,
        min_score=min_score,
        folder=folder,
        tag=tag,
        date=date,
        source_emails=source_emails,
        q=q,
        global_scope=(date is not None or tag is not None),
        limit=limit,
        offset=offset,
    )
    out: list[LeadSummary] = []
    for item in page:
        d = item["dossier"]
        # Freshness backstop: TODAY'S gate is re-applied to the page (cheap — a
        # page, not the store) and any drift heals the column, so a rule change
        # corrects the next request up-front.
        fresh = regate_recommendation(d)
        if fresh != item["recommendation"]:
            _store.set_filter_columns(
                item["email_hash"], fresh, d.potential_score, bool(d.person.bound)
            )
        if recommendation is not None and fresh != recommendation:
            continue  # drifted out of an explicit filter — healed above
        if recommendation is None and fresh == "skip":
            continue  # drifted to junk under the default actionable view
        out.append(_lead_summary(
            d,
            recommendation=fresh,
            source=source_by_email.get(d.email, ""),
            folder=item["folder"],
            tags=item["tags"],
            created_at=item["created_at"],
        ))
    response.headers["X-Total-Count"] = str(total)
    return out


@router.get("/export.csv", dependencies=[Depends(require_api_key)])
def export_leads(
    recommendation: str | None = Query(default=None, description="export only this recommendation"),
    emails: str | None = Query(default=None, description="comma-separated emails to export (selected only)"),
    full: bool = Query(default=False, description="export all columns (default: email+name only)"),
    folder: str | None = Query(default=None, description="only leads in this folder"),
    tag: str | None = Query(default=None, description="only leads carrying this tag"),
    date: str | None = Query(default=None, description="only leads extracted on this date (YYYY-MM-DD)"),
    source: str | None = Query(default=None, description="only leads from this query run"),
    bound: bool | None = Query(default=None, description="only bound/unbound leads"),
    min_score: float | None = Query(default=None, ge=0, le=10, description="minimum potential score"),
    q: str | None = Query(default=None, description="identity text search (company/email/person/role)"),
    user: User = Depends(get_current_user),
) -> Response:
    """Download researched leads as CSV (openable in Excel/Sheets).

    Default: email + name + company only, ACTIONABLE only (``skip``/junk is not
    exported unless asked for — a dead domain is not an outreach lead).
    ?full=true: all columns. ?emails=a@b.com,c@d.com: only selected emails.
    folder/tag/date/source/bound/min_score/q run the SAME user-view filters the
    Companies list applies (in SQL), so export always matches what the screen
    shows. Default selects what the DEFAULT Companies list shows (unfiled inbox
    + actionable only); pass ``folder=*`` for every place.
    """
    email_list = [e.strip() for e in emails.split(",") if e.strip()] if emails else None
    scope = _view_scope(user)
    source_emails = (
        {e for e, s in _job_source_by_email(
            user_id=scope["user_id"], is_admin=scope["is_admin"],
            include_legacy=scope["include_legacy"],
        ).items() if s == source} if source else None
    )
    csv_data = export_csv(
        _store,
        recommendation=recommendation,
        emails=email_list,
        full=full,
        folder=folder,
        tag=tag,
        date=date,
        source_emails=source_emails,
        bound=bound,
        min_score=min_score,
        q=q,
        user_id=scope["user_id"],
        is_admin=scope["is_admin"],
        include_legacy=scope["include_legacy"],
    )
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
    )


@router.post("/clear-junk", dependencies=[Depends(require_api_key)])
def clear_junk(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Bulk-remove every dossier the list HIDES by default — re-gated ``skip``
    junk (dead/expired domain, non-construction, sub-threshold, generic mail).

    The user's data-management control: junk that never shows in the actionable
    view can be purged in one action, so dead domains stop hiding in the data.
    Returns exactly how many were removed (never silent — CLAUDE.md §6). The
    same emails are dropped from the discovery cache so a future run does not
    re-discover them.

    Per-user isolation: a non-admin's clear only purges their OWN junk.
    """
    from app.lead_research.scoring import regate_recommendation

    scope = _view_scope(user)
    removed: list[str] = []
    for d in _store.list_all(**scope):
        if regate_recommendation(d) == "skip":
            if _store.delete(d.email, reason="junk"):
                removed.append(d.email)
    if removed:
        from app.lead_research.service import PendingLeadsStore

        PendingLeadsStore(db_path=_store._db_path).remove(removed)
        logger.info("POST /leads/clear-junk -> removed %d junk dossier(s)", len(removed))
    return {"removed": len(removed), "emails": removed}


@router.post("/pending/sweep", dependencies=[Depends(require_api_key)])
def sweep_pending() -> dict[str, Any]:
    """One-pass pre-screen of the discovery cache — the "proper total fix".

    Flags every pending lead the research stage would reject anyway (free-mail,
    or no MX on the email's domain) as ``dead`` so it is NEVER served and never
    burns a research slot on a future run, and removes pending rows that are
    already-researched dossiers. Uses the SAME cheap gates the pipeline uses
    (free-mail triage + fast native MX check) — no AI credits spent. Returns
    honest per-bucket counts so the user sees exactly what was kept vs removed
    (CLAUDE.md §6).
    """
    from app.lead_research.service import PendingLeadsStore

    pending = PendingLeadsStore(db_path=_store._db_path)
    stats = pending.sweep_known_dead(dossier_store=_store)
    logger.info("POST /leads/pending/sweep -> %s", stats)
    return stats


@router.put("/{email}/organize", dependencies=[Depends(require_api_key)])
def organize_lead(email: str, body: OrganizeIn,
                  user: User = Depends(get_current_user)) -> LeadSummary:
    """Set ONE lead's folder + tags (user organization metadata, Phase B).

    Pure metadata — the researched dossier is untouched. The row delete paths
    (delete / clear-junk / dead-domain cleanup) clear it automatically with
    the row. Returns the updated lead row.

    Per-user isolation: a non-admin can only organize their OWN dossier; the
    source label (which run produced it) comes from their own jobs only.
    """
    if not _user_owns_dossier(email, user):
        raise HTTPException(status_code=404, detail=f"no dossier for {email}")
    if not _store.set_meta(email, folder=body.folder, tags=body.tags):
        raise HTTPException(status_code=404, detail=f"no dossier for {email}")
    dossier = _store.get(email)
    from app.lead_research.scoring import regate_recommendation

    rec = regate_recommendation(dossier) if dossier else "skip"
    logger.info("PUT /leads/%s/organize -> folder=%r tags=%r", email, body.folder, body.tags)
    meta = _store.get_meta(email)
    scope = _view_scope(user)
    return _lead_summary(
        dossier,
        recommendation=rec,
        source=_job_source_by_email(
            user_id=scope["user_id"], is_admin=scope["is_admin"],
            include_legacy=scope["include_legacy"],
        ).get(email, ""),
        folder=meta.folder if meta else "",
        tags=meta.tags if meta else [],
        created_at=_store.research_dates().get(_email_hash(email), ""),
    )


@router.post("/organize/rename", dependencies=[Depends(require_api_key)])
def organize_rename(body: OrganizeRenameIn,
                    user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Rename a folder or tag across EVERY dossier; honest count (not silent).

    Per-user isolation: a non-admin's rename only touches their own dossiers.
    """
    if body.kind not in ("folder", "tag"):
        raise HTTPException(status_code=422, detail="kind must be 'folder' or 'tag'")
    scope = _view_scope(user)
    updated = (
        _store.rename_folder(body.from_, body.to, **scope)
        if body.kind == "folder"
        else _store.rename_tag(body.from_, body.to, **scope)
    )
    logger.info("POST /leads/organize/rename %s %r->%r updated=%d", body.kind, body.from_, body.to, updated)
    return {"kind": body.kind, "from": body.from_, "to": body.to, "updated": updated}


@router.post("/organize/clear", dependencies=[Depends(require_api_key)])
def organize_clear(body: OrganizeClearIn,
                   user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Remove a folder or tag value from EVERY dossier; honest count.

    Per-user isolation: a non-admin's clear only touches their own dossiers.
    """
    if body.kind not in ("folder", "tag"):
        raise HTTPException(status_code=422, detail="kind must be 'folder' or 'tag'")
    scope = _view_scope(user)
    updated = (
        _store.clear_folder(body.value, **scope)
        if body.kind == "folder"
        else _store.clear_tag(body.value, **scope)
    )
    logger.info("POST /leads/organize/clear %s %r -> %d", body.kind, body.value, updated)
    return {"kind": body.kind, "value": body.value, "updated": updated}


@router.get("/folders", response_model=FoldersOut, dependencies=[Depends(require_api_key)])
def list_folders(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """The organization mailbox overview for the Companies rail.

    Returns ``{folders: [...], unfiled, total}`` — the persisted (possibly
    empty) folder groups, PLUS how many leads sit in the default Unfiled inbox
    vs every dossier in the store. ``unfiled`` is the DEFAULT Companies view;
    ``total`` feeds the "All" chip + Dashboard. The catalog self-heals names
    that only ever rode on leads, so nothing is lost.

    Per-user isolation: a non-admin sees only their OWN folders/counts.
    """
    return _store.folder_catalog(**_view_scope(user))


@router.get("/dates", response_model=list[str], dependencies=[Depends(require_api_key)])
def list_dates(user: User = Depends(get_current_user)) -> list[str]:
    """Every extraction date (``YYYY-MM-DD``) present on a VISIBLE dossier, newest
    first.

    Date is the GLOBAL recall dimension ("kis tareekh ko kya nikla") — it sees
    leads inside folders too, unlike the default Companies view. Computed in SQL
    (one DISTINCT GROUP, no full-store scan); a date whose only rows are
    admin-hidden drops out of the dropdown.

    Per-user isolation: a non-admin sees only dates from their OWN dossiers.
    """
    return _store.distinct_dates(**_view_scope(user))


@router.get("/tags", response_model=list[dict[str, Any]], dependencies=[Depends(require_api_key)])
def list_tags(user: User = Depends(get_current_user)) -> list[dict[str, Any]]:
    """Global tag counts for the USER views (hidden + skip/ungated excluded).

    One SQL GROUP BY over ``json_each`` — the Companies screen's tag chip bar
    counts tags without downloading the whole list just to tally them.

    Per-user isolation: a non-admin only counts their OWN dossiers' tags.
    """
    return _store.tag_counts(**_view_scope(user))


@router.get("/sources", response_model=list[str], dependencies=[Depends(require_api_key)])
def list_sources(user: User = Depends(get_current_user)) -> list[str]:
    """Every query-run source label, for the Run/source dropdown.

    Derived from the persisted job results, so the dropdown stays complete even
    when the current page only shows a few rows.

    Per-user isolation: a non-admin sees only labels from their OWN jobs.
    """
    scope = _view_scope(user)
    return sorted({s for s in _job_source_by_email(
        user_id=scope["user_id"], is_admin=scope["is_admin"],
        include_legacy=scope["include_legacy"],
    ).values() if s})


@router.post("/folders", response_model=FolderOut, status_code=201,
             dependencies=[Depends(require_api_key)])
def create_folder(body: FolderCreate, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Create a persisted (possibly empty) folder, e.g. ``Monday data``.

    Idempotent: a duplicate name returns ``created=False`` with the existing
    row — the user's "make a folder, click it, then move leads in" flow.
    The folder is OWNED by the creating account (same isolation as data):
    it appears on their dashboard and the admin panel's drill-down, never on
    another account's Companies rail.
    """
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="folder name required")
    created = _store.create_folder(name, user_id=user.id)
    item = next(
        (f for f in _store.list_folders(**_view_scope(user)) if f["name"] == name),
        None,
    )
    if item is None:  # should be impossible after create — never silent
        raise HTTPException(status_code=500, detail="folder created but not listed")
    logger.info(
        "POST /leads/folders %r created=%s count=%d", name, created, item["count"]
    )
    return {
        "name": item["name"],
        "created_at": item["created_at"],
        "count": item["count"],
        "created": created,
    }


@router.delete("/{email}", dependencies=[Depends(require_api_key)])
def delete_lead(email: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Delete ONE lead (dossier + discovery cache) — the user's per-lead data
    management. A dismissed lead is gone and will not be re-researched.

    Per-user isolation: a non-admin can only delete their OWN dossier.
    """
    if not _user_owns_dossier(email, user):
        raise HTTPException(status_code=404, detail=f"no dossier for {email}")
    if not _store.delete(email):
        raise HTTPException(status_code=404, detail=f"no dossier for {email}")
    # Also drop it from the discovery cache so a future run does not re-discover
    # it and re-burn credits on an address the user chose to dismiss.
    from app.lead_research.service import PendingLeadsStore

    PendingLeadsStore(db_path=_store._db_path).remove([email])
    logger.info("DELETE /leads/%s -> lead removed", email)
    return {"email": email, "deleted": True}


@router.get("/{email}", response_model=LeadDetail, dependencies=[Depends(require_api_key)])
def get_lead(email: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Full researched dossier for one lead (evidence + user metadata).

    Per-user isolation: a non-admin can only open their OWN dossier.
    """
    if not _user_owns_dossier(email, user):
        raise HTTPException(status_code=404, detail=f"no dossier for {email}")
    dossier = _store.get(email)
    if dossier is None:
        raise HTTPException(status_code=404, detail=f"no dossier for {email}")
    out = dossier.to_dict()
    out["created_at"] = _store.research_dates().get(_email_hash(email), "")
    meta = _store.get_meta(email)
    if meta:
        out["folder"] = meta.folder
        out["tags"] = meta.tags
    else:
        out["folder"] = ""
        out["tags"] = []
    return out
