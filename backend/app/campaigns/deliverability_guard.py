"""Persist hard-bounce events and hold senders after repeated failures."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone


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

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def record_hard_bounce(self, account_id: int, email: str, *,
                           now: datetime | None = None) -> bool:
        """Hold an account after two distinct hard bounces in 24 hours.

        Returns True if the account is now held. Duplicate DSNs for one
        recipient cannot inflate the count.
        """
        if account_id <= 0 or "@" not in email:
            return False
        now = now or datetime.now(timezone.utc)
        when = now.astimezone(timezone.utc).isoformat()
        cutoff = (now - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO hard_bounce_events "
                         "(account_id,email,occurred_at) VALUES (?,?,?)",
                         (account_id, email.strip().lower(), when))
            count = conn.execute(
                "SELECT COUNT(*) FROM hard_bounce_events WHERE account_id=? "
                "AND occurred_at>=?", (account_id, cutoff),
            ).fetchone()[0]
            if count >= 2:
                conn.execute("INSERT OR IGNORE INTO sender_holds "
                             "(account_id,held_at,reason) VALUES (?,?,?)",
                             (account_id, when, "two hard bounces in 24 hours"))
            return conn.execute(
                "SELECT 1 FROM sender_holds WHERE account_id=?",
                (account_id,),
            ).fetchone() is not None

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
