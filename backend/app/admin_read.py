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


def _json_object(value: Any) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
