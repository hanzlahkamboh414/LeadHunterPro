"""EmailAccountStore — the user's connected SENDING accounts (Phase E2).

Lives in the SAME users.db as the accounts themselves: OAuth tokens are
user-domain credentials (auth concern), never lead data. Tokens are
encrypted at rest (Fernet — see crypto.py); the list view NEVER returns
them, and the API layer only decrypts at the moment of use.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

from app.email_accounts.crypto import decrypt_text, encrypt_text

_INIT_LOCK = threading.RLock()


class EmailAccountStore:
    """SQLite persistence for connected email accounts (provider = google)."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            # Same default location as UserStore (auth domain, users.db).
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "output", "users.db"
            )
        self._db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        with _INIT_LOCK:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            conn = self._conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS email_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'google',
                    email TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    access_token TEXT NOT NULL DEFAULT '',
                    refresh_token TEXT NOT NULL DEFAULT '',
                    token_expires_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'connected',
                    scopes TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, provider, email)
                )
            """)
            # E3 rows predate the scopes column (gmail.readonly arrived in
            # Phase E4) — plain ALTER; scopes are not secret, no rebuild.
            cols = [r[1] for r in conn.execute("PRAGMA table_info(email_accounts)")]
            if "scopes" not in cols:
                conn.execute(
                    "ALTER TABLE email_accounts "
                    "ADD COLUMN scopes TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()
            conn.close()

    # -- Connect / refresh ------------------------------------------------

    def connect(
        self, user_id: str, email: str, *, display_name: str = "",
        access_token: str = "", refresh_token: str = "",
        token_expires_at: str = "", scopes: str = "",
    ) -> dict[str, Any]:
        """Upsert one connected account (re-connecting an existing Gmail
        refreshes its tokens — the UNIQUE (user, provider, email) row is
        reused, never duplicated). Returns the stored row WITHOUT tokens."""
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO email_accounts
                (user_id, provider, email, display_name, access_token,
                 refresh_token, token_expires_at, status, scopes)
            VALUES (?, 'google', ?, ?, ?, ?, ?, 'connected', ?)
            ON CONFLICT (user_id, provider, email) DO UPDATE SET
                display_name = excluded.display_name,
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                token_expires_at = excluded.token_expires_at,
                status = 'connected',
                scopes = excluded.scopes,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, email, display_name or "",
             encrypt_text(access_token), encrypt_text(refresh_token),
             token_expires_at, scopes or ""),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM email_accounts WHERE user_id = ? AND email = ?",
            (user_id, email),
        ).fetchone()
        conn.close()
        return self._public_row({
            "id": row[0], "user_id": user_id, "provider": "google",
            "email": email, "display_name": display_name,
            "status": "connected", "scopes": scopes or "",
        })

    def update_tokens(self, account_id: int, user_id: str, *,
                      access_token: str, token_expires_at: str) -> bool:
        """Refresh an account's access token (the refresh token is permanent
        until revoked — Google only returns it on first consent)."""
        conn = self._conn()
        cur = conn.execute(
            "UPDATE email_accounts SET access_token = ?, token_expires_at = ?, "
            "status = 'connected', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ?",
            (encrypt_text(access_token), token_expires_at, account_id, user_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def mark_status(self, account_id: int, user_id: str, status: str) -> bool:
        """connected | expired | revoked — a revoked account pauses its
        campaigns (Phase E3) instead of silently failing every send."""
        conn = self._conn()
        cur = conn.execute(
            "UPDATE email_accounts SET status = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ?",
            (status, account_id, user_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    # -- Read / delete ----------------------------------------------------

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """The user's connected accounts — NO tokens ever leave the store."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, provider, email, display_name, status, scopes, created_at "
            "FROM email_accounts WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        ).fetchall()
        conn.close()
        return [
            {"id": r[0], "provider": r[1], "email": r[2],
             "display_name": r[3] or "", "status": r[4],
             "scopes": r[5] or "", "created_at": r[6] or ""}
            for r in rows
        ]

    def get_credentials(self, account_id: int, user_id: str) -> dict[str, Any] | None:
        """DECRYPTED tokens for ONE account — the only door, used at send
        time. None when the account is missing or not the caller's."""
        conn = self._conn()
        row = conn.execute(
            "SELECT email, access_token, refresh_token, token_expires_at, "
            "status, scopes FROM email_accounts WHERE id = ? AND user_id = ?",
            (account_id, user_id),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "email": row[0],
            "access_token": decrypt_text(row[1]),
            "refresh_token": decrypt_text(row[2]),
            "token_expires_at": row[3] or "",
            "status": row[4],
            "scopes": row[5] or "",
        }

    def delete(self, account_id: int, user_id: str) -> bool:
        """Disconnect — the row (and its tokens) is removed entirely."""
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM email_accounts WHERE id = ? AND user_id = ?",
            (account_id, user_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    @staticmethod
    def _public_row(base: dict[str, Any]) -> dict[str, Any]:
        """The shape every caller outside this module sees — tokens never
        appear in it, by construction."""
        return {
            "id": base["id"], "provider": base["provider"],
            "email": base["email"], "display_name": base["display_name"],
            "status": base["status"], "scopes": base.get("scopes", ""),
            "created_at": "",
        }


# Module-level singleton (the deps._user_store pattern — tests override it).
_store: EmailAccountStore | None = None


def get_email_store() -> EmailAccountStore:
    global _store
    if _store is None:
        _store = EmailAccountStore()
    return _store
