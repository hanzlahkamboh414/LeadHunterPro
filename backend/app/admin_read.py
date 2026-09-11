"""Read-only projections of the original lead-research SQLite database."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.lead_research.models import LeadDossier
from app.lead_research.scoring import regate_recommendation
from app.schemas.admin import AdminDashboardOut, AdminJobSummary


def default_database_path() -> str:
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "output", "lead_research.db")
    )


class AdminReadRepository:
    """Build admin metrics without changing the existing lead system."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_database_path()

    def dashboard(self) -> AdminDashboardOut:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            dossiers = self._dossiers(conn)
            pending = self._pending_counts(conn)
            jobs = self._jobs(conn)
        finally:
            conn.close()

        recommendations = {"contact_now": 0, "nurture": 0, "skip": 0}
        risks = {
            "source_errors": 0,
            "missing_evidence": 0,
            "unbound_contacts": 0,
            "missing_company": 0,
        }
        for dossier in dossiers:
            recommendation = regate_recommendation(dossier)
            recommendations[recommendation] = recommendations.get(recommendation, 0) + 1
            if dossier.source_errors:
                risks["source_errors"] += 1
            if not dossier.sources_checked:
                risks["missing_evidence"] += 1
            if not dossier.person.bound:
                risks["unbound_contacts"] += 1
            if not dossier.company.name:
                risks["missing_company"] += 1

        job_counts: dict[str, int] = {}
        recent_jobs: list[AdminJobSummary] = []
        for job in jobs:
            state = str(job["state"])
            job_counts[state] = job_counts.get(state, 0) + 1
            recent_jobs.append(
                AdminJobSummary(
                    id=str(job["id"]),
                    query=_json_object(job["query_json"]),
                    state=state,
                    error=str(job["error"] or ""),
                    created_at=str(job["created_at"]),
                    updated_at=str(job["updated_at"]),
                    elapsed_s=float(job["elapsed_s"] or 0),
                )
            )

        return AdminDashboardOut(
            generated_at=datetime.now(timezone.utc).isoformat(),
            database_path=self.db_path,
            dossiers_total=len(dossiers),
            recommendations=recommendations,
            pending=pending,
            jobs=job_counts,
            risks=risks,
            recent_jobs=recent_jobs[:10],
        )

    @staticmethod
    def _dossiers(conn: sqlite3.Connection) -> list[LeadDossier]:
        rows = conn.execute(
            "SELECT dossier_json FROM dossiers ORDER BY updated_at DESC"
        ).fetchall()
        return [LeadDossier.from_dict(json.loads(row[0])) for row in rows]

    @staticmethod
    def _pending_counts(conn: sqlite3.Connection) -> dict[str, int]:
        rows = conn.execute(
            "SELECT dead, COUNT(*) FROM pending_leads GROUP BY dead"
        ).fetchall()
        counts = {"active": 0, "dead": 0, "total": 0}
        for dead, count in rows:
            key = "dead" if int(dead) else "active"
            counts[key] = int(count)
            counts["total"] += int(count)
        return counts

    @staticmethod
    def _jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT id, query_json, state, error, created_at, updated_at, elapsed_s
            FROM jobs
            ORDER BY created_at DESC
            """
        ).fetchall()


    def deleted_log(self, limit: int = 100) -> dict[str, Any]:
        """Which emails the user deleted, when, and why (the admin audit trail).

        Read from the ``deleted_leads`` table the store writes on every Delete /
        Junk sweep — the admin screen's "kon kon c email delete ki".
        """
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT email, deleted_at, reason FROM deleted_leads "
                "ORDER BY deleted_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM deleted_leads").fetchone()[0]
        finally:
            conn.close()
        return {
            "total": int(total),
            "deleted": [{"email": r[0], "deleted_at": r[1], "reason": r[2]} for r in rows],
        }

    def pending_emails(self, limit: int = 100) -> dict[str, Any]:
        """The discovery cache (``pending_leads``) — emails waiting to be
        researched, with their dead/retry state ("kon kon c email cache ma hai").

        Read-only projection; the store owns writes. ``attempted_at`` /
        ``attempt_count`` reflect the re-enrichment cooldown state.
        """
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            tally = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN dead THEN 1 ELSE 0 END), 0) "
                "FROM pending_leads"
            ).fetchone()
            total = int(tally[0] or 0)
            dead = int(tally[1] or 0)
            rows = conn.execute(
                "SELECT email, location, dead, COALESCE(attempted_at,''), "
                "COALESCE(attempt_count, 0) FROM pending_leads "
                "ORDER BY dead ASC, created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            conn.close()
        return {
            "total": total,
            "active": total - dead,
            "dead": dead,
            "rows": [
                {
                    "email": r[0],
                    "location": r[1] or "",
                    "dead": bool(r[2]),
                    "attempted_at": r[3],
                    "attempt_count": r[4],
                }
                for r in rows
            ],
        }


def search_cache_status() -> dict[str, Any]:
    """Read-only status of the provider-neutral search-result cache.

    Separate db file (``backend/output/search_cache.db``). Shows row counts,
    TTL config, and the most recently cached queries — the admin's visibility
    into how many paid queries the disk cache has saved.
    """
    from app.core.config import settings
    from app.search_providers.cache import default_db_path, get_search_cache

    path = getattr(settings, "SEARCH_CACHE_DB", "") or default_db_path()
    cache = get_search_cache()
    stats = cache.stats() if cache else {}
    conn = None
    search_rows = extract_rows = 0
    top: list[tuple] = []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        search_rows = conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
        extract_rows = conn.execute("SELECT COUNT(*) FROM extract_cache").fetchone()[0]
        top = conn.execute(
            "SELECT query, max_results, fetched_at FROM search_cache "
            "ORDER BY fetched_at DESC LIMIT 20"
        ).fetchall()
    except Exception:  # noqa: BLE001 — cache file may not exist yet (never fatal)
        pass
    finally:
        if conn is not None:
            conn.close()
    return {
        "path": path,
        "search_rows": int(search_rows),
        "extract_rows": int(extract_rows),
        "search_ttl_days": int(getattr(settings, "SEARCH_CACHE_TTL_DAYS", 14)),
        "extract_ttl_days": int(getattr(settings, "SEARCH_CACHE_EXTRACT_TTL_DAYS", 30)),
        "hit_rate": float(stats.get("hit_rate", 0.0)),
        "top_queries": [
            {"query": r[0], "max_results": r[1], "fetched_at": r[2]} for r in top
        ],
    }


def purge_search_cache() -> int:
    """Remove TTL-expired search/extract entries; returns how many were removed.

    The search cache is disposable infrastructure (a paid query re-fills it), so
    this is the admin's safe maintenance action. Pending/discovery data is NOT
    touched.
    """
    from app.search_providers.cache import get_search_cache

    cache = get_search_cache()
    return cache.purge_expired() if cache else 0


def _json_object(value: Any) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
