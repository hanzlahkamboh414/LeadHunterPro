"""CampaignStore — campaigns + their per-lead send queue (Phase E3).

Lives in its own ``campaigns.db``: outreach is neither an auth concern
(users.db) nor research data (lead_research.db); it is the user's outbound
workflow. Cross-domain links are by value only — ``account_id`` points into
email_accounts, lead emails point into dossiers — so each store stays
independently migratable.

All timestamps are ISO-8601 UTC strings. The STORE is pure persistence +
queries; every decision (when to send, what to do on 429) lives in
scheduler.py so it can be tested with injected clocks.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

from app.auth.models import _now

_INIT_LOCK = threading.RLock()

CAMPAIGN_STATUSES = ("scheduled", "running", "paused", "completed")


class CampaignStore:
    """SQLite persistence for campaigns and their send queue."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "output", "campaigns.db"
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
                CREATE TABLE IF NOT EXISTS campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    account_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    paused_reason TEXT NOT NULL DEFAULT '',
                    resume_at TEXT NOT NULL DEFAULT '',
                    start_at TEXT NOT NULL,
                    daily_limit INTEGER NOT NULL DEFAULT 30,
                    delay_min_s INTEGER NOT NULL DEFAULT 180,
                    delay_max_s INTEGER NOT NULL DEFAULT 420,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS campaign_sends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    subject TEXT NOT NULL DEFAULT '',
                    sent_at TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    UNIQUE (campaign_id, email)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sends_campaign "
                "ON campaign_sends (campaign_id, state)"
            )
            conn.commit()
            conn.close()

    # -- Create -------------------------------------------------------

    def create(
        self, user_id: str, *, account_id: int, name: str, subject: str,
        body: str, emails: list[str], start_at: str,
        daily_limit: int = 30, delay_min_s: int = 180, delay_max_s: int = 420,
    ) -> dict[str, Any]:
        """One campaign + its pending send queue (insertion order = send
        order). Duplicate emails within the list are de-duplicated here."""
        seen: list[str] = []
        for e in emails:
            if e and e not in seen:
                seen.append(e)
        now = _now()
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO campaigns (user_id, account_id, name, subject, body, "
            "status, start_at, daily_limit, delay_min_s, delay_max_s, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?)",
            (user_id, account_id, name, subject, body, start_at,
             int(daily_limit), int(delay_min_s), int(delay_max_s), now, now),
        )
        campaign_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO campaign_sends (campaign_id, email) VALUES (?, ?)",
            [(campaign_id, e) for e in seen],
        )
        conn.commit()
        conn.close()
        return self.get(campaign_id, user_id) or {}

    # -- Read -----------------------------------------------------------

    def get(self, campaign_id: int, user_id: str) -> dict[str, Any] | None:
        """One campaign (None when missing or not the caller's) + progress."""
        conn = self._conn()
        row = conn.execute(
            "SELECT id, user_id, account_id, name, subject, body, status, "
            "paused_reason, resume_at, start_at, daily_limit, delay_min_s, "
            "delay_max_s, created_at, updated_at FROM campaigns "
            "WHERE id = ? AND user_id = ?",
            (campaign_id, user_id),
        ).fetchone()
        counts = self._counts(conn, [campaign_id]).get(campaign_id, {})
        conn.close()
        if row is None:
            return None
        return self._row_to_campaign(row, counts)

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, user_id, account_id, name, subject, body, status, "
            "paused_reason, resume_at, start_at, daily_limit, delay_min_s, "
            "delay_max_s, created_at, updated_at FROM campaigns "
            "WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        counts = self._counts(conn, [r[0] for r in rows])
        conn.close()
        return [self._row_to_campaign(r, counts.get(r[0], {})) for r in rows]

    def sends(self, campaign_id: int, user_id: str,
              limit: int = 200) -> list[dict[str, Any]] | None:
        """The send queue (pending order first, then sent/failed). None when
        the campaign is not the caller's."""
        conn = self._conn()
        owns = conn.execute(
            "SELECT 1 FROM campaigns WHERE id = ? AND user_id = ?",
            (campaign_id, user_id),
        ).fetchone()
        if owns is None:
            conn.close()
            return None
        rows = conn.execute(
            "SELECT id, email, state, subject, sent_at, attempts, error "
            "FROM campaign_sends WHERE campaign_id = ? "
            "ORDER BY CASE state WHEN 'pending' THEN 0 ELSE 1 END, id LIMIT ?",
            (campaign_id, int(limit)),
        ).fetchall()
        conn.close()
        return [
            {"id": r[0], "email": r[1], "state": r[2], "subject": r[3],
             "sent_at": r[4] or "", "attempts": r[5], "error": r[6] or ""}
            for r in rows
        ]

    @staticmethod
    def _counts(conn: sqlite3.Connection,
                campaign_ids: list[int]) -> dict[int, dict[str, int]]:
        if not campaign_ids:
            return {}
        marks = ",".join("?" * len(campaign_ids))
        rows = conn.execute(
            f"SELECT campaign_id, state, COUNT(*) FROM campaign_sends "
            f"WHERE campaign_id IN ({marks}) GROUP BY campaign_id, state",
            campaign_ids,
        ).fetchall()
        out: dict[int, dict[str, int]] = {}
        for cid, state, n in rows:
            out.setdefault(cid, {"pending": 0, "sent": 0, "failed": 0})[state] = n
        for cid in campaign_ids:
            out.setdefault(cid, {"pending": 0, "sent": 0, "failed": 0})
        return out

    @staticmethod
    def _row_to_campaign(r, counts: dict[str, int]) -> dict[str, Any]:
        return {
            "id": r[0], "user_id": r[1], "account_id": r[2], "name": r[3],
            "subject": r[4], "body": r[5], "status": r[6],
            "paused_reason": r[7] or "", "resume_at": r[8] or "",
            "start_at": r[9], "daily_limit": r[10],
            "delay_min_s": r[11], "delay_max_s": r[12],
            "created_at": r[13], "updated_at": r[14],
            "pending": counts.get("pending", 0),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
        }

    # -- Scheduler support ------------------------------------------------

    def running_campaigns(self) -> list[dict[str, Any]]:
        """Every RUNNING campaign across users — the scheduler's worklist."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, user_id, account_id, name, subject, body, status, "
            "paused_reason, resume_at, start_at, daily_limit, delay_min_s, "
            "delay_max_s, created_at, updated_at FROM campaigns "
            "WHERE status = 'running' ORDER BY id ASC"
        ).fetchall()
        conn.close()
        return [self._row_to_campaign(r, {}) for r in rows]

    def promote_scheduled(self, now_iso: str) -> list[int]:
        """start_at reached -> running. Returns the promoted ids (logged)."""
        conn = self._conn()
        rows = conn.execute(
            "UPDATE campaigns SET status = 'running', updated_at = ? "
            "WHERE status = 'scheduled' AND start_at <= ? "
            "RETURNING id",
            (now_iso, now_iso),
        ).fetchall()
        conn.commit()
        conn.close()
        return [r[0] for r in rows]

    def next_pending(self, campaign_id: int) -> dict[str, Any] | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT id, email, attempts FROM campaign_sends "
            "WHERE campaign_id = ? AND state = 'pending' ORDER BY id LIMIT 1",
            (campaign_id,),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {"id": row[0], "email": row[1], "attempts": row[2]}

    def last_sent_at(self, campaign_id: int) -> str:
        conn = self._conn()
        row = conn.execute(
            "SELECT MAX(sent_at) FROM campaign_sends "
            "WHERE campaign_id = ? AND state = 'sent'", (campaign_id,),
        ).fetchone()
        conn.close()
        return (row[0] or "") if row else ""

    def sent_today_for_account(self, account_id: int, today: str) -> int:
        """Sent count for the ACCOUNT (all campaigns) on one UTC date — the
        daily cap is per Gmail account, not per campaign."""
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM campaign_sends s JOIN campaigns c "
            "ON s.campaign_id = c.id WHERE c.account_id = ? "
            "AND s.state = 'sent' AND substr(s.sent_at, 1, 10) = ?",
            (account_id, today),
        ).fetchone()
        conn.close()
        return row[0] if row else 0

    def already_sent_emails(self, user_id: str,
                            emails: list[str]) -> set[str]:
        """Which of these emails were ALREADY sent to (any campaign, this
        user)? Used at create time to never double-email a lead."""
        if not emails:
            return set()
        conn = self._conn()
        marks = ",".join("?" * len(emails))
        rows = conn.execute(
            f"SELECT DISTINCT s.email FROM campaign_sends s "
            f"JOIN campaigns c ON s.campaign_id = c.id "
            f"WHERE c.user_id = ? AND s.state = 'sent' AND s.email IN ({marks})",
            [user_id, *emails],
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}

    # -- Status transitions ------------------------------------------------

    def set_status(self, campaign_id: int, *, status: str,
                   paused_reason: str = "", resume_at: str = "") -> bool:
        if status not in CAMPAIGN_STATUSES:
            raise ValueError(f"invalid campaign status: {status}")
        conn = self._conn()
        cur = conn.execute(
            "UPDATE campaigns SET status = ?, paused_reason = ?, resume_at = ?, "
            "updated_at = ? WHERE id = ?",
            (status, paused_reason, resume_at, _now(), campaign_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def resume_rate_limited(self, now_iso: str) -> list[int]:
        """429 cooldown over -> back to running (the user's pause/resume
        design: rate-limit pauses are temporary, self-healing)."""
        conn = self._conn()
        rows = conn.execute(
            "UPDATE campaigns SET status = 'running', paused_reason = '', "
            "resume_at = '', updated_at = ? WHERE status = 'paused' "
            "AND paused_reason = 'rate_limited' AND resume_at != '' "
            "AND resume_at <= ? RETURNING id",
            (now_iso, now_iso),
        ).fetchall()
        conn.commit()
        conn.close()
        return [r[0] for r in rows]

    def resume_for_accounts(self, account_ids: list[int]) -> int:
        """Account healthy again -> its account-paused campaigns resume
        (the user's design: disconnect → paused → reconnect → resume)."""
        if not account_ids:
            return 0
        conn = self._conn()
        marks = ",".join("?" * len(account_ids))
        cur = conn.execute(
            f"UPDATE campaigns SET status = 'running', paused_reason = '', "
            f"resume_at = '', updated_at = ? WHERE status = 'paused' "
            f"AND paused_reason = 'account' AND account_id IN ({marks})",
            [_now(), *account_ids],
        )
        conn.commit()
        conn.close()
        return cur.rowcount

    def account_paused_accounts(self) -> list[tuple[int, str]]:
        """DISTINCT (account_id, user_id) of campaigns paused for account
        reasons — the scheduler health-checks these to auto-resume."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT DISTINCT account_id, user_id FROM campaigns "
            "WHERE status = 'paused' AND paused_reason = 'account'"
        ).fetchall()
        conn.close()
        return [(r[0], r[1]) for r in rows]

    def delete(self, campaign_id: int, user_id: str) -> bool:
        conn = self._conn()
        owns = conn.execute(
            "SELECT 1 FROM campaigns WHERE id = ? AND user_id = ?",
            (campaign_id, user_id),
        ).fetchone()
        if owns is None:
            conn.close()
            return False
        conn.execute("DELETE FROM campaign_sends WHERE campaign_id = ?",
                     (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
        conn.commit()
        conn.close()
        return True

    # -- Send outcomes ------------------------------------------------------

    def mark_sent(self, send_id: int, *, subject: str, sent_at: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE campaign_sends SET state = 'sent', subject = ?, "
            "sent_at = ?, error = '' WHERE id = ?",
            (subject, sent_at, send_id),
        )
        conn.commit()
        conn.close()

    def mark_failed(self, send_id: int, *, error: str) -> None:
        """attempts++ and state=failed — a send that exhausted its retries
        (pending sends left alone are simply retried next pass)."""
        conn = self._conn()
        conn.execute(
            "UPDATE campaign_sends SET state = 'failed', error = ?, "
            "attempts = attempts + 1 WHERE id = ?",
            (error, send_id),
        )
        conn.commit()
        conn.close()

    def bump_attempts(self, send_id: int) -> None:
        """Count one failed try but stay 'pending' — the scheduler's retry
        path (attempts cap is enforced by the scheduler, not here)."""
        conn = self._conn()
        conn.execute(
            "UPDATE campaign_sends SET attempts = attempts + 1 WHERE id = ?",
            (send_id,),
        )
        conn.commit()
        conn.close()

    def mark_completed_if_drained(self, campaign_id: int) -> bool:
        """No pending sends left -> completed (failed ones don't block it)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM campaign_sends "
            "WHERE campaign_id = ? AND state = 'pending'", (campaign_id,),
        ).fetchone()
        if row and row[0] == 0:
            conn.execute(
                "UPDATE campaigns SET status = 'completed', updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (_now(), campaign_id),
            )
            conn.commit()
            conn.close()
            return True
        conn.close()
        return False


# Module-level singleton (the deps._user_store pattern — tests override it).
_store: CampaignStore | None = None


def get_campaign_store() -> CampaignStore:
    global _store
    if _store is None:
        _store = CampaignStore()
    return _store
