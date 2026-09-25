"""Activity log — who did what, for the admin panel.

Records the user-visible actions that matter operationally (login, logout,
signup, search) in the SAME users.db the accounts live in: this is auth-domain
data, never lead data, so it stays out of lead_research.db.

Actions are recorded at the API seams (auth endpoints, job submission) — the
same places that already log the event; this persists it for the admin view.
"""

from __future__ import annotations

import os
import sqlite3

from app.auth.models import _now
from app.core.db_paths import operational_db_path


def get_activity() -> "ActivityStore":
    """Lazy singleton — the same pattern as auth.dependencies._user_store.

    One shared instance so every recording seam (login/logout in auth.py,
    search submission in leads.py) writes to the same users.db activity log.
    """
    global _activity_store
    if _activity_store is None:
        _activity_store = ActivityStore()
    return _activity_store


_activity_store: "ActivityStore | None" = None


class ActivityStore:
    """SQLite persistence for the user activity log."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = operational_db_path(os.path.join(
                os.path.dirname(__file__), "..", "..", "output", "users.db"
            ))
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                tenant_id TEXT
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(activity_log)")}
        if "tenant_id" not in columns:
            conn.execute("ALTER TABLE activity_log ADD COLUMN tenant_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_created "
            "ON activity_log (created_at DESC)"
        )
        conn.commit()
        conn.close()

    def record(
        self, user_id: str, username: str, action: str, detail: str = "",
        tenant_id: str | None = None,
    ) -> None:
        """Append one activity row. Never raises into the request path — a
        failed audit write must not break the action it is describing."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO activity_log "
                "(user_id, username, action, detail, created_at, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, action, detail, _now(), tenant_id),
            )
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def list(
        self, limit: int = 200, user_id: str = "",
        tenant_id: str | None = None,
    ) -> list[dict]:
        """Recent activity, newest first — all users, or one user's history."""
        if os.environ.get("LEADHUNTER_MULTI_TENANT_ENABLED") == "1" and not tenant_id:
            raise ValueError("tenant_id is required in multi-tenant mode")
        conn = sqlite3.connect(self._db_path)
        try:
            clauses: list[str] = []
            args: list[str | int] = []
            if user_id:
                clauses.append("user_id = ?")
                args.append(user_id)
            if tenant_id:
                clauses.append("tenant_id = ?")
                args.append(tenant_id)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            args.append(int(limit))
            cur = conn.execute(
                "SELECT id, user_id, username, action, detail, created_at, "
                f"tenant_id FROM activity_log{where} ORDER BY id DESC LIMIT ?",
                args,
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r[0],
                "user_id": r[1],
                "username": r[2],
                "action": r[3],
                "detail": r[4] or "",
                "created_at": r[5],
                "tenant_id": r[6],
            }
            for r in rows
        ]
