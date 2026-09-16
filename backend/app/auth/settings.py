"""Login-auth toggle — one persisted setting in users.db.

The admin can turn the login page OFF from the admin panel. The site then
opens straight into the normal user UI (token-less visitors run as the
shared account), and the admin panel is reachable ONLY through the secret
URL + password gate (/admin4269). Default is ON — the toggle is opt-in,
never a silent behavior change.
"""

from __future__ import annotations

import os
import sqlite3


class AuthSettings:
    """SQLite persistence for auth settings (key/value, same users.db as the
    accounts — the auth domain owns one DB per concern)."""

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
        conn.execute(
            "CREATE TABLE IF NOT EXISTS auth_settings ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()

    def get(self, key: str, default: str = "") -> str:
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT value FROM auth_settings WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT INTO auth_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
        conn.close()

    def auth_enabled(self) -> bool:
        """Login required? Default True — missing row = the safe default."""
        return self.get("auth_enabled", "1") != "0"

    def set_auth_enabled(self, enabled: bool) -> None:
        self.set("auth_enabled", "1" if enabled else "0")

    def gmail_inbox_enabled(self) -> bool:
        """The Gmail-inbox app interface (browse/read/send) usable?

        The ADDRESS EXPORT always works — this flag only gates the Gmail-like
        browsing interface (2026-09-16: disabled while the batch transport is
        being stabilized under load; the export is the production feature).
        Default True — missing row = the feature is ON.
        """
        return self.get("gmail_inbox_enabled", "1") != "0"

    def set_gmail_inbox_enabled(self, enabled: bool) -> None:
        self.set("gmail_inbox_enabled", "1" if enabled else "0")


_instance: AuthSettings | None = None


def get_settings() -> AuthSettings:
    """Lazy singleton — resolved through the module attribute so tests can
    monkeypatch ``_instance`` (same indirection as dependencies._user_store)."""
    global _instance
    if _instance is None:
        _instance = AuthSettings()
    return _instance
