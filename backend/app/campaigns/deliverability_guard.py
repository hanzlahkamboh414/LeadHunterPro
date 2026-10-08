"""Track send outcomes and hold accounts after three consecutive failures."""

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
            conn.execute("""CREATE TABLE IF NOT EXISTS delivery_attempts (
                account_id INTEGER NOT NULL,
                send_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                outcome TEXT NOT NULL,
                PRIMARY KEY (account_id, send_id)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_attempts_order "
                         "ON delivery_attempts(account_id, sent_at DESC, send_id DESC)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def record_send_accepted(self, account_id: int, send_id: int, *,
                             now: datetime | None = None) -> None:
        """Record Gmail/SMTP acceptance in send order (not proof of delivery)."""
        if account_id <= 0 or send_id <= 0:
            return
        when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO delivery_attempts "
                "(account_id,send_id,sent_at,outcome) VALUES (?,?,?,'accepted')",
                (account_id, send_id, when),
            )

    def record_delivery_failure(self, account_id: int, send_id: int, *,
                                sent_at: datetime | None = None) -> bool:
        """Count hard bounces and policy blocks by original send order.

        Repeated notices update the same attempt. A late notice for an older
        send cannot override a newer accepted send. Existing holds stay put.
        """
        if account_id <= 0 or send_id <= 0:
            return False
        when = (sent_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO delivery_attempts "
                "(account_id,send_id,sent_at,outcome) VALUES (?,?,?,'failed') "
                "ON CONFLICT(account_id,send_id) DO UPDATE SET outcome='failed'",
                (account_id, send_id, when),
            )
            latest = [row[0] for row in conn.execute(
                "SELECT outcome FROM delivery_attempts WHERE account_id=? "
                "ORDER BY sent_at DESC, send_id DESC LIMIT 3", (account_id,),
            )]
            if len(latest) == 3 and all(outcome == "failed" for outcome in latest):
                conn.execute("INSERT OR IGNORE INTO sender_holds "
                             "(account_id,held_at,reason) VALUES (?,?,?)",
                             (account_id, when, "three consecutive delivery failures"))
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
