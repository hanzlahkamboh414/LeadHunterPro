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
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "output", "users.db"
            )
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
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_created "
            "ON activity_log (created_at DESC)"
        )
        conn.commit()
        conn.close()

    def record(self, user_id: str, username: str, action: str, detail: str = "") -> None:
        """Append one activity row. Never raises into the request path — a
        failed audit write must not break the action it is describing."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO activity_log (user_id, username, action, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, username, action, detail, _now()),
            )
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def list(self, limit: int = 200, user_id: str = "") -> list[dict]:
        """Recent activity, newest first — all users, or one user's history."""
        conn = sqlite3.connect(self._db_path)
        if user_id:
            cur = conn.execute(
                "SELECT id, user_id, username, action, detail, created_at "
                "FROM activity_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, int(limit)),
            )
        else:
            cur = conn.execute(
                "SELECT id, user_id, username, action, detail, created_at "
                "FROM activity_log ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
        rows = cur.fetchall()
        conn.close()
        return [
            {
                "id": r[0],
                "user_id": r[1],
                "username": r[2],
                "action": r[3],
                "detail": r[4] or "",
                "created_at": r[5],
            }
            for r in rows
        ]
