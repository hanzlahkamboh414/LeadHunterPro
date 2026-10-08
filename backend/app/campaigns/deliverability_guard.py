"""Persist hard-bounce streaks and explicit provider holds."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone


class DeliverabilityGuard:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS hard_bounce_events (
                account_id INTEGER NOT NULL,
                email TEXT NOT NULL COLLATE NOCASE,
                occurred_at TEXT NOT NULL,
                PRIMARY KEY (account_id, email)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS sender_holds (
                account_id INTEGER PRIMARY KEY,
                held_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS hard_bounce_streaks (
                account_id INTEGER PRIMARY KEY,
                consecutive_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )""")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def record_hard_bounce(self, account_id: int, email: str, *,
                           now: datetime | None = None) -> bool:
        """Hold an account after three consecutive distinct hard bounces.

        An accepted send resets the streak. Duplicate DSNs for one recipient
        cannot inflate it. Provider policy blocks use ``hold_account`` and
        still stop immediately.
        """
        if account_id <= 0 or "@" not in email:
            return False
        now = now or datetime.now(timezone.utc)
        when = now.astimezone(timezone.utc).isoformat()
        with self._conn() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO hard_bounce_events "
                "(account_id,email,occurred_at) VALUES (?,?,?)",
                (account_id, email.strip().lower(), when),
            ).rowcount
            if inserted:
                conn.execute(
                    "INSERT INTO hard_bounce_streaks "
                    "(account_id,consecutive_count,updated_at) VALUES (?,1,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET "
                    "consecutive_count=consecutive_count+1,updated_at=excluded.updated_at",
                    (account_id, when),
                )
            count = conn.execute(
                "SELECT consecutive_count FROM hard_bounce_streaks "
                "WHERE account_id=?", (account_id,),
            ).fetchone()
            if count is not None and count[0] >= 3:
                conn.execute("INSERT OR IGNORE INTO sender_holds "
                             "(account_id,held_at,reason) VALUES (?,?,?)",
                             (account_id, when, "three consecutive hard bounces"))
            return conn.execute(
                "SELECT 1 FROM sender_holds WHERE account_id=?",
                (account_id,),
            ).fetchone() is not None

    def record_send_accepted(self, account_id: int, *,
                             now: datetime | None = None) -> None:
        """Reset a hard-bounce streak after the provider accepts a send.

        Acceptance is not proof of recipient delivery; a later DSN can still
        reclassify that message. This method never clears an existing hold.
        """
        if account_id <= 0:
            return
        when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO hard_bounce_streaks "
                "(account_id,consecutive_count,updated_at) VALUES (?,0,?) "
                "ON CONFLICT(account_id) DO UPDATE SET "
                "consecutive_count=0,updated_at=excluded.updated_at",
                (account_id, when),
            )

    def is_held(self, account_id: int) -> bool:
        with self._conn() as conn:
            return conn.execute(
                "SELECT 1 FROM sender_holds WHERE account_id=?",
                (account_id,),
            ).fetchone() is not None

    def hold_account(self, account_id: int, reason: str, *,
                     now: datetime | None = None) -> None:
        """Keep a rejected sender stopped until a person reviews it."""
        if account_id <= 0:
            return
        when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sender_holds (account_id,held_at,reason) "
                "VALUES (?,?,?)", (account_id, when, reason[:300]))
