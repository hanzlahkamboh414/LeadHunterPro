"""CampaignStore — campaigns + their per-lead send queue (Phase E3/E4).

Lives in its own ``campaigns.db``: outreach is neither an auth concern
(users.db) nor research data (lead_research.db); it is the user's outbound
workflow. Cross-domain links are by value only — ``account_id`` points into
email_accounts, lead emails point into dossiers — so each store stays
independently migratable.

Phase E4 adds the follow-up ladder and reply detection:

* ``campaign_followups`` — a campaign's step 1..N definitions (after_days,
  subject, body). Step 0 is the campaign's own subject/body.
* ``campaign_sends`` gains ``step`` + ``not_before``: a follow-up row is
  only QUEUED after its predecessor was sent (not_before = sent_at +
  after_days), and only while the lead has not replied.
* ``campaign_replies`` — one row per (campaign, lead) that replied; replies
  cancel every pending follow-up for that lead.
* ``reply_checks`` — per-account last inbox-check time (throttling).

Phase E5 adds multi-account sending + AI personalization:

* ``campaign_accounts`` — the 1..N sending accounts of a campaign (the
  campaigns.account_id column stays as the primary, for display and as the
  legacy single-account value). Every campaign's primary account is
  backfilled here at boot, so a campaign ALWAYS has at least one row.
* ``campaign_sends.account_id`` — which account ACTUALLY sent that row
  (0 = not sent yet / pre-E5 row; account-scoped queries fall back to the
  campaign's primary for those legacy rows).
* ``campaigns.ai_personalize`` — per-campaign flag: prepend an AI-written
  opening line built from the lead's VERIFIED dossier evidence.
* ``campaign_hooks`` — the generated opening line per (campaign, lead),
  cached so a retry never re-calls the AI. A stored empty hook means
  "generated, nothing honest to say" (never re-asked).

All timestamps are ISO-8601 UTC strings. The STORE is pure persistence +
queries; every decision (when to send, what to do on 429) lives in
scheduler.py so it can be tested with injected clocks.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.auth.models import _now
from app.campaigns.tracking import is_repeat_view, is_sender_selfcheck

_INIT_LOCK = threading.RLock()

CAMPAIGN_STATUSES = ("scheduled", "running", "paused", "completed")

#: campaign_sends.state values. 'skipped' = a follow-up dropped because the
#: lead replied (never an error — it is the system working as designed).
SEND_STATES = ("pending", "sent", "failed", "skipped")


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
                    early_resume_date TEXT NOT NULL DEFAULT '',
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
                    step INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending',
                    subject TEXT NOT NULL DEFAULT '',
                    sent_at TEXT NOT NULL DEFAULT '',
                    not_before TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    UNIQUE (campaign_id, email, step)
                )
            """)
            # E3 rows predate step/not_before (and carried UNIQUE
            # (campaign_id, email)) — rebuild, copying every row forward.
            cols = [r[1] for r in conn.execute("PRAGMA table_info(campaign_sends)")]
            if "step" not in cols:
                conn.execute("ALTER TABLE campaign_sends RENAME TO campaign_sends_old")
                conn.execute("""
                    CREATE TABLE campaign_sends (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        campaign_id INTEGER NOT NULL,
                        email TEXT NOT NULL,
                        step INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL DEFAULT 'pending',
                        subject TEXT NOT NULL DEFAULT '',
                        sent_at TEXT NOT NULL DEFAULT '',
                        not_before TEXT NOT NULL DEFAULT '',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        error TEXT NOT NULL DEFAULT '',
                        UNIQUE (campaign_id, email, step)
                    )
                """)
                conn.execute(
                    "INSERT INTO campaign_sends (campaign_id, email, step, state, "
                    "subject, sent_at, not_before, attempts, error) "
                    "SELECT campaign_id, email, 0, state, subject, sent_at, '', "
                    "attempts, error FROM campaign_sends_old"
                )
                conn.execute("DROP TABLE campaign_sends_old")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS campaign_followups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    step INTEGER NOT NULL,
                    after_days INTEGER NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    UNIQUE (campaign_id, step)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS campaign_replies (
                    campaign_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    UNIQUE (campaign_id, email)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reply_checks (
                    account_id INTEGER PRIMARY KEY,
                    last_check TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS campaign_accounts (
                    campaign_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    UNIQUE (campaign_id, account_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS campaign_hooks (
                    campaign_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    hook TEXT NOT NULL DEFAULT '',
                    UNIQUE (campaign_id, email)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS campaign_drafts (
                    campaign_id INTEGER NOT NULL,
                    email TEXT NOT NULL COLLATE NOCASE,
                    step INTEGER NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    PRIMARY KEY (campaign_id, email, step)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS campaign_bounces (
                    campaign_id INTEGER NOT NULL,
                    email TEXT NOT NULL COLLATE NOCASE,
                    account_id INTEGER NOT NULL DEFAULT 0,
                    bounced_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (campaign_id, email)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS campaign_email_checks (
                    email TEXT PRIMARY KEY COLLATE NOCASE,
                    status TEXT NOT NULL CHECK(status IN ('ready', 'hold', 'invalid')),
                    reason TEXT NOT NULL DEFAULT '',
                    checked_at TEXT NOT NULL
                )
            """)
            conn.execute("""CREATE TABLE IF NOT EXISTS mails_so_validation_usage (
                day TEXT PRIMARY KEY,
                requests INTEGER NOT NULL DEFAULT 0
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS builtin_smtp_probe_usage (
                day TEXT PRIMARY KEY,
                requests INTEGER NOT NULL DEFAULT 0
            )""")
            # E5 additive migrations (plain ALTERs — no rebuild needed).
            cols = [r[1] for r in conn.execute("PRAGMA table_info(campaigns)")]
            if "ai_personalize" not in cols:
                conn.execute("ALTER TABLE campaigns "
                             "ADD COLUMN ai_personalize INTEGER NOT NULL DEFAULT 0")
            if "ai_compose" not in cols:
                conn.execute("ALTER TABLE campaigns "
                             "ADD COLUMN ai_compose INTEGER NOT NULL DEFAULT 0")
            if "ai_signature" not in cols:
                conn.execute("ALTER TABLE campaigns "
                             "ADD COLUMN ai_signature TEXT NOT NULL DEFAULT ''")
            if "early_resume_date" not in cols:
                conn.execute("ALTER TABLE campaigns "
                             "ADD COLUMN early_resume_date TEXT NOT NULL DEFAULT ''")
            cols = [r[1] for r in conn.execute("PRAGMA table_info(campaign_sends)")]
            if "account_id" not in cols:
                conn.execute("ALTER TABLE campaign_sends "
                             "ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0")
            # Open tracking (E5 polish): first open time + total count.
            if "opened_at" not in cols:
                conn.execute("ALTER TABLE campaign_sends "
                             "ADD COLUMN opened_at TEXT NOT NULL DEFAULT ''")
            if "opened_count" not in cols:
                conn.execute("ALTER TABLE campaign_sends "
                             "ADD COLUMN opened_count INTEGER NOT NULL DEFAULT 0")
            # Dedupe anchor: when the last COUNTED open happened, so a
            # re-fetched pixel (same view) doesn't inflate the count.
            if "last_open_at" not in cols:
                conn.execute("ALTER TABLE campaign_sends "
                             "ADD COLUMN last_open_at TEXT NOT NULL DEFAULT ''")
            # Every campaign's primary account becomes a campaign_accounts row
            # (idempotent) — pre-E5 campaigns keep working unchanged.
            conn.execute(
                "INSERT OR IGNORE INTO campaign_accounts (campaign_id, account_id) "
                "SELECT id, account_id FROM campaigns"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sends_campaign "
                "ON campaign_sends (campaign_id, state)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS campaign_recipient_history (
                    user_id TEXT NOT NULL,
                    email TEXT NOT NULL COLLATE NOCASE,
                    campaign_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, email)
                )
            """)
            conn.execute(
                "INSERT OR IGNORE INTO campaign_recipient_history "
                "(user_id, email, campaign_id, account_id, sent_at) "
                "SELECT c.user_id, lower(s.email), s.campaign_id, "
                "CASE WHEN s.account_id > 0 THEN s.account_id ELSE c.account_id END, "
                "s.sent_at FROM campaign_sends s "
                "JOIN campaigns c ON c.id = s.campaign_id "
                "WHERE s.step = 0 AND s.state = 'sent' "
                "ORDER BY s.sent_at, s.id"
            )
            conn.commit()
            conn.close()

    # -- Create -------------------------------------------------------

    def create(
        self, user_id: str, *, account_id: int, name: str, subject: str,
        body: str, emails: list[str], start_at: str,
        daily_limit: int = 30, delay_min_s: int = 180, delay_max_s: int = 420,
        followups: list[dict[str, Any]] | None = None,
        account_ids: list[int] | None = None,
        ai_personalize: bool = False,
        ai_compose: bool = False,
        ai_signature: str = "",
        recipient_limit: int | None = None,
    ) -> dict[str, Any]:
        """One campaign + its pending step-0 send queue (insertion order =
        send order) + its follow-up definitions (steps 1..N). Duplicate
        emails within the list are de-duplicated here. ``account_ids`` adds
        EXTRA sending accounts beyond the primary (E5 multi-account); the
        primary always comes first and duplicates drop."""
        seen: list[str] = []
        for e in emails:
            clean = e.strip().lower()
            if clean and clean not in seen:
                seen.append(clean)
        accounts = [account_id]
        for a in account_ids or []:
            if a and a not in accounts:
                accounts.append(int(a))
        now = _now()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        # Reserve recipients inside the write transaction. The API's preview
        # check alone can race a second campaign creation.
        claimed = {row[0] for row in conn.execute(
            "SELECT DISTINCT lower(s.email) FROM campaign_sends s "
            "JOIN campaigns c ON c.id = s.campaign_id "
            "WHERE c.user_id = ? AND s.step = 0", (user_id,),
        )}
        claimed.update(row[0] for row in conn.execute(
            "SELECT email FROM campaign_recipient_history WHERE user_id = ?",
            (user_id,),
        ))
        seen = [e for e in seen if e.lower() not in claimed]
        if recipient_limit is not None:
            seen = seen[:recipient_limit]
            if len(seen) < recipient_limit:
                conn.rollback()
                conn.close()
                raise ValueError("Some leads became unavailable; refresh and try again")
        if not seen:
            conn.rollback()
            conn.close()
            raise ValueError("all recipients are already queued or emailed")
        cur = conn.execute(
            "INSERT INTO campaigns (user_id, account_id, name, subject, body, "
            "status, start_at, daily_limit, delay_min_s, delay_max_s, "
            "ai_personalize, ai_compose, ai_signature, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, account_id, name, subject, body, start_at,
             int(daily_limit), int(delay_min_s), int(delay_max_s),
             1 if ai_personalize else 0, 1 if ai_compose else 0,
             ai_signature.strip(), now, now),
        )
        campaign_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO campaign_sends (campaign_id, email) VALUES (?, ?)",
            [(campaign_id, e) for e in seen],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO campaign_accounts (campaign_id, account_id) "
            "VALUES (?, ?)",
            [(campaign_id, a) for a in accounts],
        )
        for i, fu in enumerate(followups or []):
            conn.execute(
                "INSERT INTO campaign_followups (campaign_id, step, after_days, "
                "subject, body) VALUES (?, ?, ?, ?, ?)",
                (campaign_id, i + 1, int(fu["after_days"]),
                 fu["subject"], fu["body"]),
            )
        conn.commit()
        conn.close()
        return self.get(campaign_id, user_id) or {}

    # -- Read -----------------------------------------------------------

    _CAMPAIGN_COLS = ("id, user_id, account_id, name, subject, body, status, "
                      "paused_reason, resume_at, start_at, daily_limit, "
                      "delay_min_s, delay_max_s, ai_personalize, "
                      "created_at, updated_at, early_resume_date, "
                      "ai_compose, ai_signature")

    def get(self, campaign_id: int, user_id: str) -> dict[str, Any] | None:
        """One campaign (None when missing or not the caller's) + progress."""
        conn = self._conn()
        row = conn.execute(
            f"SELECT {self._CAMPAIGN_COLS} FROM campaigns "
            f"WHERE id = ? AND user_id = ?",
            (campaign_id, user_id),
        ).fetchone()
        counts = self._counts(conn, [campaign_id]).get(campaign_id, {})
        conn.close()
        if row is None:
            return None
        out = self._row_to_campaign(row, counts)
        out["account_ids"] = self.campaign_accounts(campaign_id)
        return out

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            f"SELECT {self._CAMPAIGN_COLS} FROM campaigns "
            f"WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        counts = self._counts(conn, [r[0] for r in rows])
        conn.close()
        out = [self._row_to_campaign(r, counts.get(r[0], {})) for r in rows]
        for c in out:
            c["account_ids"] = self.campaign_accounts(c["id"])
        return out

    def sends(self, campaign_id: int, user_id: str,
              limit: int = 5000) -> list[dict[str, Any]] | None:
        """The send queue (pending order first, then sent/failed/skipped).
        None when the campaign is not the caller's."""
        conn = self._conn()
        owns = conn.execute(
            "SELECT 1 FROM campaigns WHERE id = ? AND user_id = ?",
            (campaign_id, user_id),
        ).fetchone()
        if owns is None:
            conn.close()
            return None
        rows = conn.execute(
            "SELECT s.id, s.email, s.step, s.state, s.subject, s.sent_at, "
            "s.not_before, s.attempts, s.error, s.account_id, s.opened_at, "
            "s.opened_count, cr.received_at "
            "FROM campaign_sends s "
            "LEFT JOIN campaign_replies cr "
            "ON cr.campaign_id = s.campaign_id AND cr.email = s.email "
            "WHERE s.campaign_id = ? "
            "ORDER BY CASE s.state WHEN 'pending' THEN 0 ELSE 1 END, s.id "
            "LIMIT ?",
            (campaign_id, int(limit)),
        ).fetchall()
        conn.close()
        return [
            {"id": r[0], "email": r[1], "step": r[2], "state": r[3],
             "subject": r[4], "sent_at": r[5] or "", "not_before": r[6] or "",
             "attempts": r[7], "error": r[8] or "", "account_id": r[9] or 0,
             "opened_at": r[10] or "", "opened_count": r[11] or 0,
             "replied_at": r[12] or ""}
            for r in rows
        ]

    def record_bounce(self, campaign_id: int, email: str, *, account_id: int = 0,
                      reason: str = "") -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO campaign_bounces "
            "(campaign_id, email, account_id, bounced_at, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (campaign_id, email.strip().lower(), account_id, _now(), reason[:300]),
        )
        conn.commit()
        conn.close()

    def bounces(self, campaign_id: int, user_id: str) -> list[dict[str, Any]] | None:
        conn = self._conn()
        owns = conn.execute("SELECT 1 FROM campaigns WHERE id = ? AND user_id = ?",
                            (campaign_id, user_id)).fetchone()
        if owns is None:
            conn.close()
            return None
        rows = conn.execute(
            "SELECT email, account_id, bounced_at, reason FROM campaign_bounces "
            "WHERE campaign_id = ? ORDER BY bounced_at DESC", (campaign_id,),
        ).fetchall()
        conn.close()
        return [{"email": r[0], "account_id": r[1], "bounced_at": r[2],
                 "reason": r[3]} for r in rows]

    def followups(self, campaign_id: int) -> list[dict[str, Any]]:

        """The campaign's follow-up definitions, in step order."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT step, after_days, subject, body FROM campaign_followups "
            "WHERE campaign_id = ? ORDER BY step ASC",
            (campaign_id,),
        ).fetchall()
        conn.close()
        return [{"step": r[0], "after_days": r[1], "subject": r[2],
                 "body": r[3]} for r in rows]

    def followup(self, campaign_id: int, step: int) -> dict[str, Any] | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT step, after_days, subject, body FROM campaign_followups "
            "WHERE campaign_id = ? AND step = ?",
            (campaign_id, step),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {"step": row[0], "after_days": row[1], "subject": row[2],
                "body": row[3]}

    def followup_after_days(self, campaign_id: int, step: int) -> int | None:
        """The after_days of one follow-up step (None = no such step)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT after_days FROM campaign_followups "
            "WHERE campaign_id = ? AND step = ?",
            (campaign_id, step),
        ).fetchone()
        conn.close()
        return int(row[0]) if row else None

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
            out.setdefault(cid, _empty_counts())[state] = n
        for cid in campaign_ids:
            out.setdefault(cid, _empty_counts())
        replies = conn.execute(
            f"SELECT campaign_id, COUNT(*) FROM campaign_replies "
            f"WHERE campaign_id IN ({marks}) GROUP BY campaign_id",
            campaign_ids,
        ).fetchall()
        for cid, n in replies:
            out.setdefault(cid, _empty_counts())["replied"] = n
        return out

    @staticmethod
    def _row_to_campaign(r, counts: dict[str, int]) -> dict[str, Any]:
        return {
            "id": r[0], "user_id": r[1], "account_id": r[2], "name": r[3],
            "subject": r[4], "body": r[5], "status": r[6],
            "paused_reason": r[7] or "", "resume_at": r[8] or "",
            "start_at": r[9], "daily_limit": r[10],
            "delay_min_s": r[11], "delay_max_s": r[12],
            "ai_personalize": bool(r[13]),
            "created_at": r[14], "updated_at": r[15],
            "early_resume_date": r[16] or "",
            "ai_compose": bool(r[17]),
            "ai_signature": r[18] or "",
            "pending": counts.get("pending", 0),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            "replied": counts.get("replied", 0),
        }

    # -- Scheduler support ------------------------------------------------

    def running_campaigns(self) -> list[dict[str, Any]]:
        """Every RUNNING campaign across users — the scheduler's worklist."""
        conn = self._conn()
        rows = conn.execute(
            f"SELECT {self._CAMPAIGN_COLS} FROM campaigns "
            f"WHERE status = 'running' ORDER BY id ASC"
        ).fetchall()
        conn.close()
        return [self._row_to_campaign(r, {}) for r in rows]

    def campaign_accounts(self, campaign_id: int) -> list[int]:
        """The campaign's sending accounts, primary first (E5)."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT account_id FROM campaign_accounts WHERE campaign_id = ? "
            "ORDER BY rowid ASC",
            (campaign_id,),
        ).fetchall()
        primary = conn.execute(
            "SELECT account_id FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        conn.close()
        ids = [r[0] for r in rows]
        # The primary (campaigns.account_id) always leads, even if a legacy
        # row somehow landed out of order.
        if primary and primary[0] in ids:
            ids.remove(primary[0])
            ids.insert(0, primary[0])
        return ids

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

    def next_pending(self, campaign_id: int,
                     now_iso: str = "", *,
                     verified_only: bool = False) -> dict[str, Any] | None:
        """The next send due: oldest pending row whose not_before (if any)
        has passed. An empty ``now_iso`` ignores not_before entirely (the
        E3 store-call shape — used by tests/admin inspection)."""
        where = "state = 'pending'"
        params: list[Any] = [campaign_id]
        if now_iso:
            where += " AND (not_before = '' OR not_before <= ?)"
            params.append(now_iso)
        if verified_only:
            where += (" AND EXISTS (SELECT 1 FROM campaign_email_checks v "
                      "WHERE v.email = campaign_sends.email AND v.status = 'ready' "
                      "AND v.reason IN ('mails.so:deliverable', "
                      "'first-party:reply-confirmed'))")
        conn = self._conn()
        row = conn.execute(
            f"SELECT id, email, step, attempts FROM campaign_sends "
            f"WHERE campaign_id = ? AND {where} ORDER BY id LIMIT 1",
            params,
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {"id": row[0], "email": row[1], "step": row[2], "attempts": row[3]}

    def next_pending_at(self, campaign_id: int) -> str:
        """Earliest scheduled pending follow-up, if there is one."""
        conn = self._conn()
        row = conn.execute(
            "SELECT MIN(not_before) FROM campaign_sends "
            "WHERE campaign_id = ? AND state = 'pending' AND not_before != ''",
            (campaign_id,),
        ).fetchone()
        conn.close()
        return (row[0] or "") if row else ""

    def email_check_status(self, email: str) -> str | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT status, checked_at, reason FROM campaign_email_checks WHERE email = ?",
            ((email or "").strip().lower(),),
        ).fetchone()
        conn.close()
        if row and row[0] == "ready" and row[2] not in (
                "mails.so:deliverable", "first-party:reply-confirmed"):
            return "hold"
        if row and row[0] == "ready":
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            if row[1] <= cutoff:
                return None
        return row[0] if row else None

    def invalid_checked_emails(self, emails: list[str]) -> set[str]:
        if not emails:
            return set()
        conn = self._conn()
        found: set[str] = set()
        for offset in range(0, len(emails), 500):
            chunk = [str(email).strip().lower() for email in emails[offset:offset + 500]]
            marks = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT lower(email) FROM campaign_email_checks "
                f"WHERE status = 'invalid' AND email IN ({marks})", chunk,
            ).fetchall()
            found.update(row[0] for row in rows)
        conn.close()
        return found

    def emails_to_verify(self, *, limit: int = 100,
                         retry_before: str = "",
                         ready_before: str = "",
                         provider_retry_before: str = "",
                         builtin_retry_before: str = "") -> list[str]:
        """Distinct queued recipients without a result, plus old holds."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT lower(s.email) FROM campaign_sends s "
            "LEFT JOIN campaign_email_checks v ON v.email = s.email "
            "WHERE s.state = 'pending' AND (v.email IS NULL "
            "OR v.status = 'invalid' "
            "OR (v.status = 'hold' AND v.checked_at <= ? "
            "AND v.reason NOT LIKE 'mails.so:%' "
            "AND v.reason NOT LIKE 'builtin:%') "
            "OR (v.status = 'hold' AND v.checked_at <= ? "
            "AND v.reason LIKE 'mails.so:%') "
            "OR (v.status = 'hold' AND v.checked_at <= ? "
            "AND (v.reason LIKE 'builtin:%' OR v.reason LIKE 'smtp-probe:%')) "
            "OR (v.status = 'ready' AND (v.checked_at <= ? "
            "OR v.reason NOT IN ('mails.so:deliverable', "
            "'first-party:reply-confirmed')))) "
            "GROUP BY lower(s.email) "
            "ORDER BY COALESCE(v.checked_at, '') ASC, MIN(s.step), MIN(s.id) LIMIT ?",
            (retry_before, provider_retry_before, builtin_retry_before,
             ready_before, int(limit)),
        ).fetchall()
        conn.close()
        return [row[0] for row in rows]

    def reserve_mails_so_validation(self, day: str, *, limit: int) -> bool:
        """Persist a conservative per-day request cap before network I/O."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            used = conn.execute(
                "SELECT requests FROM mails_so_validation_usage WHERE day=?",
                (day,),
            ).fetchone()
            if used and used[0] >= limit:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO mails_so_validation_usage(day,requests) VALUES (?,1) "
                "ON CONFLICT(day) DO UPDATE SET requests=requests+1",
                (day,),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def reserve_builtin_smtp_probe(self, day: str, *, limit: int) -> bool:
        """Cap no-DATA SMTP probes per UTC day before touching remote MXes."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            used = conn.execute(
                "SELECT requests FROM builtin_smtp_probe_usage WHERE day=?",
                (day,),
            ).fetchone()
            if used and used[0] >= limit:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO builtin_smtp_probe_usage(day,requests) VALUES (?,1) "
                "ON CONFLICT(day) DO UPDATE SET requests=requests+1",
                (day,),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def save_email_check(self, email: str, status: str, reason: str) -> None:
        if status not in {"ready", "hold", "invalid"}:
            raise ValueError("bad email verification status")
        conn = self._conn()
        conn.execute(
            "INSERT INTO campaign_email_checks(email, status, reason, checked_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET "
            "status=excluded.status, reason=excluded.reason, "
            "checked_at=excluded.checked_at",
            ((email or "").strip().lower(), status, reason[:150], _now()),
        )
        conn.commit()
        conn.close()

    def last_sent_at(self, campaign_id: int) -> str:
        conn = self._conn()
        row = conn.execute(
            "SELECT MAX(sent_at) FROM campaign_sends "
            "WHERE campaign_id = ? AND state = 'sent'", (campaign_id,),
        ).fetchone()
        conn.close()
        return (row[0] or "") if row else ""

    def last_sent_at_for_account(self, account_id: int) -> str:
        """Latest send BY ONE ACCOUNT (all campaigns) — the E5 pacing input.
        Gmail's sending rhythm is per account, so the 3-7 min gap is now
        measured per account, not per campaign."""
        conn = self._conn()
        row = conn.execute(
            "SELECT MAX(s.sent_at) FROM campaign_sends s JOIN campaigns c "
            "ON s.campaign_id = c.id WHERE s.state = 'sent' AND "
            "(CASE WHEN s.account_id > 0 THEN s.account_id ELSE c.account_id END) = ?",
            (account_id,),
        ).fetchone()
        conn.close()
        return (row[0] or "") if row else ""

    def sent_today_for_account(self, account_id: int, today: str) -> int:
        """Sent count for the ACCOUNT (all campaigns) on one Pakistan date — the
        daily cap is per Gmail account, not per campaign. Pre-E5 rows (no
        send-level account) count via the campaign's primary account."""
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM campaign_sends s JOIN campaigns c "
            "ON s.campaign_id = c.id WHERE s.state = 'sent' AND "
            "(CASE WHEN s.account_id > 0 THEN s.account_id ELSE c.account_id END) = ? "
            "AND date(s.sent_at, '+5 hours') = ?",
            (account_id, today),
        ).fetchone()
        conn.close()
        return row[0] if row else 0

    def already_sent_emails(self, user_id: str,
                            emails: list[str]) -> set[str]:
        """Recipients already queued or emailed in this user's campaigns."""
        if not emails:
            return set()
        conn = self._conn()
        rows = conn.execute(
            "SELECT DISTINCT lower(s.email) FROM campaign_sends s "
            "JOIN campaigns c ON s.campaign_id = c.id "
            "WHERE c.user_id = ? AND s.step = 0", (user_id,),
        ).fetchall()
        historical = conn.execute(
            "SELECT email FROM campaign_recipient_history WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        conn.close()
        requested = {e.strip().lower() for e in emails}
        return {r[0] for r in [*rows, *historical] if r[0] in requested}

    def recipient_statuses(self, user_id: str) -> dict[str, list[str]]:
        """Campaign membership for the user's lead picker, including deleted sends."""
        conn = self._conn()
        queued = {r[0] for r in conn.execute(
            "SELECT DISTINCT lower(s.email) FROM campaign_sends s "
            "JOIN campaigns c ON c.id=s.campaign_id "
            "WHERE c.user_id=? AND s.step=0 AND s.state<>'sent'",
            (user_id,),
        )}
        emailed = {r[0] for r in conn.execute(
            "SELECT email FROM campaign_recipient_history WHERE user_id=?",
            (user_id,),
        )}
        conn.close()
        return {"queued": sorted(queued - emailed), "emailed": sorted(emailed)}

    def activity_for_user(self, user_id: str, limit: int = 100,
                          offset: int = 0) -> list[dict[str, Any]]:
        """Sent emails across campaigns, with sender and detected reply metadata."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT s.id, s.email, s.step, s.sent_at, s.subject, "
            "CASE WHEN s.account_id > 0 THEN s.account_id ELSE c.account_id END, "
            "c.id, c.name, r.received_at, r.subject "
            "FROM campaign_sends s JOIN campaigns c ON c.id = s.campaign_id "
            "LEFT JOIN campaign_replies r ON r.campaign_id = c.id AND r.email = s.email "
            "WHERE c.user_id = ? AND s.state = 'sent' "
            "ORDER BY s.sent_at DESC, s.id DESC LIMIT ? OFFSET ?",
            (user_id, int(limit), int(offset)),
        ).fetchall()
        conn.close()
        return [
            {"send_id": r[0], "email": r[1], "step": r[2],
             "sent_at": r[3] or "", "subject": r[4] or "",
             "account_id": r[5], "campaign_id": r[6],
             "campaign_name": r[7], "replied_at": r[8] or "",
             "reply_subject": r[9] or ""}
            for r in rows
        ]

    def explore_activity(
        self, user_id: str, *,
        view: str = "sent", from_date: str = "", to_date: str = "",
        account_id: int = 0, campaign_id: int = 0, email: str = "",
        limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        """Paged, owner-scoped history with totals for every matching date/account."""
        if view not in ("sent", "replied", "bounced", "followup"):
            raise ValueError("unknown activity view")
        conn = self._conn()
        try:
            scope = ""
            owner_args: list[Any] = [user_id]
            account = "CASE WHEN s.account_id > 0 THEN s.account_id ELSE c.account_id END"
            if view == "bounced":
                source = (
                    "SELECT COALESCE(s.id, 0) AS send_id, b.email, 0 AS step, "
                    "b.bounced_at AS event_at, COALESCE(s.sent_at, '') AS sent_at, "
                    "COALESCE(s.subject, '') AS subject, "
                    "CASE WHEN b.account_id > 0 THEN b.account_id ELSE c.account_id END AS account_id, "
                    "c.id AS campaign_id, c.name AS campaign_name, "
                    "'' AS replied_at, '' AS reply_subject, b.reason AS reason "
                    "FROM campaign_bounces b JOIN campaigns c ON c.id = b.campaign_id "
                    "LEFT JOIN campaign_sends s ON s.campaign_id = b.campaign_id "
                    "AND s.email = b.email AND s.step = 0 "
                    "WHERE c.user_id = ?" + scope
                )
            elif view == "replied":
                source = (
                    "SELECT s.id AS send_id, s.email, s.step, r.received_at AS event_at, "
                    "s.sent_at, s.subject, " + account + " AS account_id, "
                    "c.id AS campaign_id, c.name AS campaign_name, "
                    "r.received_at AS replied_at, r.subject AS reply_subject, '' AS reason "
                    "FROM campaign_replies r JOIN campaigns c ON c.id = r.campaign_id "
                    "JOIN campaign_sends s ON s.campaign_id = c.id AND s.email = r.email AND s.step = 0 "
                    "WHERE c.user_id = ?" + scope
                )
            else:
                step_clause = "s.step > 0" if view == "followup" else "s.step = 0"
                source = (
                    "SELECT s.id AS send_id, s.email, s.step, s.sent_at AS event_at, "
                    "s.sent_at, s.subject, " + account + " AS account_id, "
                    "c.id AS campaign_id, c.name AS campaign_name, "
                    "COALESCE(r.received_at, '') AS replied_at, "
                    "COALESCE(r.subject, '') AS reply_subject, '' AS reason "
                    "FROM campaign_sends s JOIN campaigns c ON c.id = s.campaign_id "
                    "LEFT JOIN campaign_replies r ON r.campaign_id = c.id AND r.email = s.email "
                    "WHERE c.user_id = ?" + scope +
                    " AND s.state = 'sent' AND " + step_clause
                )
            filters = ["event_at != ''"]
            values: list[Any] = []
            if from_date:
                filters.append("substr(event_at, 1, 10) >= ?")
                values.append(from_date)
            if to_date:
                filters.append("substr(event_at, 1, 10) <= ?")
                values.append(to_date)
            if account_id:
                filters.append("account_id = ?")
                values.append(account_id)
            if campaign_id:
                filters.append("campaign_id = ?")
                values.append(campaign_id)
            if email:
                filters.append("lower(email) LIKE ?")
                values.append("%" + email.strip().lower() + "%")
            filtered = "WITH activity AS (" + source + ") SELECT * FROM activity WHERE " + " AND ".join(filters)
            args = (*owner_args, *values)
            cols = [d[0] for d in conn.execute(filtered + " LIMIT 0", args).description]
            rows = conn.execute(
                filtered + " ORDER BY event_at DESC, send_id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            total, first_date = conn.execute(
                "SELECT COUNT(*), MIN(substr(event_at, 1, 10)) FROM (" + filtered + ")",
                args,
            ).fetchone()
            by_date = conn.execute(
                "SELECT substr(event_at, 1, 10), COUNT(*) FROM (" + filtered + ") "
                "GROUP BY substr(event_at, 1, 10) ORDER BY 1 DESC", args,
            ).fetchall()
            by_account = conn.execute(
                "SELECT account_id, COUNT(*) FROM (" + filtered + ") "
                "GROUP BY account_id ORDER BY 2 DESC", args,
            ).fetchall()
            return {
                "rows": [dict(zip(cols, row, strict=True)) for row in rows],
                "total": total,
                "first_date": first_date or "",
                "by_date": [{"date": day, "count": count} for day, count in by_date],
                "by_account": [{"account_id": aid, "count": count} for aid, count in by_account],
            }
        finally:
            conn.close()

    def recipient_history(
        self, send_id: int, user_id: str, *,
        campaign_id: int = 0, email: str = "",
    ) -> dict[str, Any] | None:
        """All saved send steps and outcomes for one owned campaign recipient."""
        conn = self._conn()
        try:
            scope = ""
            args = (send_id, user_id)
            if send_id:
                anchor = conn.execute(
                    "SELECT s.campaign_id, s.email, c.name FROM campaign_sends s "
                    "JOIN campaigns c ON c.id = s.campaign_id "
                    "WHERE s.id = ? AND c.user_id = ?" + scope, args,
                ).fetchone()
            else:
                anchor = conn.execute(
                    "SELECT b.campaign_id, b.email, c.name FROM campaign_bounces b "
                    "JOIN campaigns c ON c.id = b.campaign_id "
                    "WHERE b.campaign_id = ? AND b.email = ? AND c.user_id = ?",
                    (campaign_id, email.strip().lower(), user_id),
                ).fetchone()
            if anchor is None:
                return None
            campaign_id, email, campaign_name = anchor
            sends = conn.execute(
                "SELECT s.id, s.step, s.state, s.subject, s.sent_at, s.not_before, "
                "s.error, CASE WHEN s.account_id > 0 THEN s.account_id ELSE c.account_id END, "
                "COALESCE(d.body, '') "
                "FROM campaign_sends s JOIN campaigns c ON c.id = s.campaign_id "
                "LEFT JOIN campaign_drafts d ON d.campaign_id = s.campaign_id "
                "AND d.email = s.email AND d.step = s.step "
                "WHERE s.campaign_id = ? AND s.email = ? ORDER BY s.step, s.id",
                (campaign_id, email),
            ).fetchall()
            reply = conn.execute(
                "SELECT received_at, subject FROM campaign_replies "
                "WHERE campaign_id = ? AND email = ?", (campaign_id, email),
            ).fetchone()
            bounce = conn.execute(
                "SELECT bounced_at, reason FROM campaign_bounces "
                "WHERE campaign_id = ? AND email = ?", (campaign_id, email),
            ).fetchone()
            return {
                "campaign_id": campaign_id, "campaign_name": campaign_name,
                "email": email,
                "sends": [
                    {"send_id": r[0], "step": r[1], "state": r[2],
                     "subject": r[3] or "", "sent_at": r[4] or "",
                     "not_before": r[5] or "", "error": r[6] or "",
                     "account_id": r[7], "body": r[8]}
                    for r in sends
                ],
                "reply": {"received_at": reply[0], "subject": reply[1]} if reply else None,
                "bounce": {"bounced_at": bounce[0], "reason": bounce[1]} if bounce else None,
            }
        finally:
            conn.close()

    def reply_context(self, send_id: int, user_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT s.email, s.sent_at, "
            "CASE WHEN s.account_id > 0 THEN s.account_id ELSE c.account_id END, "
            "r.received_at, r.subject "
            "FROM campaign_sends s JOIN campaigns c ON c.id = s.campaign_id "
            "JOIN campaign_replies r ON r.campaign_id = c.id AND r.email = s.email "
            "WHERE s.id = ? AND c.user_id = ? AND s.state = 'sent'",
            (send_id, user_id),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {"email": row[0], "sent_at": row[1] or "",
                "account_id": row[2], "replied_at": row[3] or "",
                "reply_subject": row[4] or ""}

    def duplicate_owner(self, campaign_id: int, user_id: str,
                        email: str) -> bool:
        """True when another campaign owns this first outreach.

        A completed send wins; otherwise the oldest pending campaign wins.
        This catches queues created before cross-campaign dedup was added.
        """
        conn = self._conn()
        rows = conn.execute(
            "SELECT s.campaign_id, s.state FROM campaign_sends s "
            "JOIN campaigns c ON c.id = s.campaign_id "
            "WHERE c.user_id = ? AND lower(s.email) = ? AND s.step = 0 "
            "AND s.state IN ('pending', 'sent') ORDER BY s.campaign_id",
            (user_id, email.strip().lower()),
        ).fetchall()
        historical = conn.execute(
            "SELECT campaign_id FROM campaign_recipient_history "
            "WHERE user_id = ? AND email = ?",
            (user_id, email.strip().lower()),
        ).fetchone()
        conn.close()
        if historical is not None and historical[0] != campaign_id:
            return True
        sent_owners = [r[0] for r in rows if r[1] == "sent"]
        owner = min(sent_owners) if sent_owners else (rows[0][0] if rows else campaign_id)
        return owner != campaign_id

    def initial_sender(self, campaign_id: int, email: str) -> int | None:
        """Account that sent step 0, for keeping follow-ups on that account."""
        conn = self._conn()
        row = conn.execute(
            "SELECT CASE WHEN s.account_id > 0 THEN s.account_id "
            "ELSE c.account_id END FROM campaign_sends s "
            "JOIN campaigns c ON c.id = s.campaign_id "
            "WHERE s.campaign_id = ? AND lower(s.email) = ? "
            "AND s.step = 0 AND s.state = 'sent'",
            (campaign_id, email.strip().lower()),
        ).fetchone()
        conn.close()
        return int(row[0]) if row else None

    # -- Follow-up queueing + replies (Phase E4) ---------------------------

    def queue_followup(self, campaign_id: int, email: str, *, step: int,
                       not_before: str) -> bool:
        """Queue one follow-up row (pending, gated by not_before). No-op when
        the lead already replied or the row already exists — decisions about
        WHETHER to queue live in the scheduler."""
        conn = self._conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO campaign_sends "
            "(campaign_id, email, step, not_before) VALUES (?, ?, ?, ?)",
            (campaign_id, email, int(step), not_before),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def is_replied(self, campaign_id: int, email: str) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM campaign_replies WHERE campaign_id = ? AND email = ?",
            (campaign_id, email),
        ).fetchone()
        conn.close()
        return row is not None

    def mark_replied(self, campaign_id: int, email: str, *,
                     received_at: str, subject: str = "") -> bool:
        """Record a reply and CANCEL every pending follow-up for that lead
        (state 'skipped' — the ladder stops the moment they answer)."""
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO campaign_replies "
            "(campaign_id, email, received_at, subject) VALUES (?, ?, ?, ?)",
            (campaign_id, email, received_at, subject),
        )
        cur = conn.execute(
            "UPDATE campaign_sends SET state = 'skipped', "
            "error = 'lead replied' WHERE campaign_id = ? AND email = ? "
            "AND state = 'pending'",
            (campaign_id, email),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def purge_recipient(self, email: str) -> int:
        """Remove a confirmed invalid address from every campaign queue.

        Sent and pending rows are removed together as requested; the bounce
        outcome remains in email_outcomes.db as the audit and send block.
        """
        addr = (email or "").strip().lower()
        if not addr:
            return 0
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            campaign_ids = [row[0] for row in conn.execute(
                "SELECT DISTINCT campaign_id FROM campaign_sends "
                "WHERE lower(email) = ?", (addr,),
            )]
            removed = conn.execute(
                "DELETE FROM campaign_sends WHERE lower(email) = ?", (addr,),
            ).rowcount
            conn.execute("DELETE FROM campaign_hooks WHERE lower(email) = ?", (addr,))
            conn.execute("DELETE FROM campaign_drafts WHERE lower(email) = ?", (addr,))
            conn.execute("DELETE FROM campaign_replies WHERE lower(email) = ?", (addr,))
            for cid in campaign_ids:
                conn.execute(
                    "UPDATE campaigns SET status = 'completed', updated_at = ? "
                    "WHERE id = ? AND status = 'running' AND NOT EXISTS "
                    "(SELECT 1 FROM campaign_sends WHERE campaign_id = ? "
                    "AND state = 'pending')",
                    (_now(), cid, cid),
                )
            conn.commit()
            return removed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_skipped(self, send_id: int, *, error: str) -> None:
        """Drop one pending send without sending (e.g. a follow-up whose
        lead replied between queueing and send time)."""
        conn = self._conn()
        conn.execute(
            "UPDATE campaign_sends SET state = 'skipped', error = ? WHERE id = ?",
            (error, send_id),
        )
        conn.commit()
        conn.close()

    def accounts_with_activity(self) -> list[tuple[int, str]]:
        """DISTINCT (account, user) that actually SENT email — the
        reply-detection worklist (E5: keyed on the send's real account)."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT DISTINCT "
            "CASE WHEN s.account_id > 0 THEN s.account_id ELSE c.account_id END, "
            "c.user_id FROM campaigns c "
            "JOIN campaign_sends s ON s.campaign_id = c.id "
            "WHERE s.state = 'sent'"
        ).fetchall()
        conn.close()
        return [(r[0], r[1]) for r in rows]

    def unreplied_sent(self, account_id: int) -> list[dict[str, Any]]:
        """Sent leads on one account that have not replied (latest send per
        campaign+lead) — the addresses reply detection matches against."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT s.campaign_id, c.user_id, s.email, MAX(s.sent_at) AS sent_at "
            "FROM campaign_sends s JOIN campaigns c ON s.campaign_id = c.id "
            "LEFT JOIN campaign_replies r ON r.campaign_id = s.campaign_id "
            "AND r.email = s.email "
            "WHERE (CASE WHEN s.account_id > 0 THEN s.account_id "
            "ELSE c.account_id END) = ? AND s.state = 'sent' "
            "AND r.email IS NULL "
            "GROUP BY s.campaign_id, s.email",
            (account_id,),
        ).fetchall()
        conn.close()
        return [{"campaign_id": r[0], "user_id": r[1], "email": r[2],
                 "sent_at": r[3] or ""} for r in rows]

    def get_reply_check(self, account_id: int) -> str:
        conn = self._conn()
        row = conn.execute(
            "SELECT last_check FROM reply_checks WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        conn.close()
        return (row[0] or "") if row else ""

    def set_reply_check(self, account_id: int, now_iso: str) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO reply_checks (account_id, last_check) VALUES (?, ?) "
            "ON CONFLICT (account_id) DO UPDATE SET last_check = excluded.last_check",
            (account_id, now_iso),
        )
        conn.commit()
        conn.close()

    # -- Status transitions ------------------------------------------------

    def mark_opened(self, send_id: int, *, opened_at: str) -> bool:
        """The tracking pixel fired — count it, unless it isn't a real,
        NEW open. Only a SENT row can open, and a fire that is (a) within
        the post-send grace (the sender viewing their own Sent copy — an
        <img> request carries no viewer identity, time is the only
        signal) or (b) within the dedupe window of the last counted open
        (the mail client's image proxy re-fetching the pixel for the SAME
        view — Gmail does this several times per open) is skipped. The
        first counted open time is kept forever. A forged token naming a
        pending or failed row marks nothing."""
        conn = self._conn()
        row = conn.execute(
            "SELECT sent_at, last_open_at FROM campaign_sends "
            "WHERE id = ? AND state = 'sent'",
            (send_id,),
        ).fetchone()
        if row is None:
            conn.close()
            return False
        if is_sender_selfcheck(row[0] or "", opened_at) \
                or is_repeat_view(row[1] or "", opened_at):
            conn.close()
            return False
        cur = conn.execute(
            "UPDATE campaign_sends SET "
            "opened_count = opened_count + 1, "
            "opened_at = CASE WHEN opened_at = '' THEN ? ELSE opened_at END, "
            "last_open_at = ? "
            "WHERE id = ? AND state = 'sent'",
            (opened_at, opened_at, send_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def update_campaign(self, campaign_id: int, user_id: str, *,
                        name: str, subject: str, body: str,
                        account_id: int | None = None,
                        account_ids: list[int] | None = None,
                        emails: list[str] | None = None,
                        start_at: str | None = None,
                        daily_limit: int | None = None,
                        delay_min_s: int | None = None,
                        delay_max_s: int | None = None,
                        followups: list[dict[str, Any]] | None = None,
                        ai_personalize: bool | None = None,
                        ai_compose: bool | None = None,
                        ai_signature: str | None = None,
                        ) -> bool:
        """Edit future sends atomically; preserve sent rows and their history."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            scope = (campaign_id, user_id)
            row = conn.execute(
                "SELECT status, account_id, start_at, daily_limit, delay_min_s, "
                "delay_max_s, ai_personalize, ai_compose, ai_signature FROM campaigns "
                "WHERE id = ? AND user_id = ?",
                scope,
            ).fetchone()
            if row is None or row[0] == "completed":
                conn.rollback()
                return False
            primary = int(account_id if account_id is not None else row[1])
            new_start = start_at if start_at is not None else row[2]
            new_status = row[0]
            if start_at is not None and row[0] == "running":
                parsed = datetime.fromisoformat(start_at)
                if parsed.astimezone(timezone.utc) > datetime.now(timezone.utc):
                    new_status = "scheduled"
            conn.execute(
                "UPDATE campaigns SET name = ?, subject = ?, body = ?, "
                "account_id = ?, start_at = ?, status = ?, daily_limit = ?, "
                "delay_min_s = ?, delay_max_s = ?, ai_personalize = ?, "
                "ai_compose = ?, ai_signature = ?, "
                "early_resume_date = CASE WHEN ? THEN '' ELSE early_resume_date END, "
                "updated_at = ? WHERE id = ? AND user_id = ?",
                (name, subject, body, primary, new_start, new_status,
                 int(daily_limit if daily_limit is not None else row[3]),
                 int(delay_min_s if delay_min_s is not None else row[4]),
                 int(delay_max_s if delay_max_s is not None else row[5]),
                 int(ai_personalize if ai_personalize is not None else row[6]),
                 int(ai_compose if ai_compose is not None else row[7]),
                 ai_signature.strip() if ai_signature is not None else row[8],
                 int(start_at is not None and new_start != row[2]),
                 _now(), *scope),
            )
            conn.execute(
                "DELETE FROM campaign_drafts WHERE campaign_id = ? AND "
                "EXISTS (SELECT 1 FROM campaign_sends s WHERE "
                "s.campaign_id = campaign_drafts.campaign_id "
                "AND lower(s.email) = lower(campaign_drafts.email) "
                "AND s.step = campaign_drafts.step AND s.state = 'pending')",
                (campaign_id,),
            )
            if account_id is not None or account_ids is not None:
                existing_accounts = [r[0] for r in conn.execute(
                    "SELECT account_id FROM campaign_accounts WHERE campaign_id = ?",
                    (campaign_id,),
                )]
                extras = account_ids if account_ids is not None else existing_accounts
                wanted = list(dict.fromkeys([primary, *(a for a in extras if a != primary)]))
                conn.execute("DELETE FROM campaign_accounts WHERE campaign_id = ?", (campaign_id,))
                conn.executemany(
                    "INSERT INTO campaign_accounts (campaign_id, account_id) VALUES (?, ?)",
                    [(campaign_id, aid) for aid in wanted],
                )

            if emails is not None:
                wanted_emails = list(dict.fromkeys(e.strip().lower() for e in emails if e.strip()))
                # A sent or terminal first email stays in history and cannot
                # be turned back into a pending send by an audience edit.
                claimed_elsewhere = {
                    r[0] for r in conn.execute(
                        "SELECT DISTINCT lower(s.email) FROM campaign_sends s "
                        "JOIN campaigns c ON c.id = s.campaign_id "
                        "WHERE c.user_id = ? AND s.step = 0 AND "
                        "(s.state = 'sent' OR "
                        "(s.state = 'pending' AND s.campaign_id != ?))",
                        (user_id, campaign_id),
                    )
                }
                claimed_elsewhere.update(r[0] for r in conn.execute(
                    "SELECT email FROM campaign_recipient_history WHERE user_id = ?",
                    (user_id,),
                ))
                terminal_here = {
                    r[0] for r in conn.execute(
                        "SELECT email FROM campaign_sends WHERE campaign_id = ? "
                        "AND step = 0 AND state IN ('failed', 'skipped')",
                        (campaign_id,),
                    )
                }
                desired = [e for e in wanted_emails
                           if e not in claimed_elsewhere and e not in terminal_here]
                keep = set(desired)
                for (send_id, email) in conn.execute(
                    "SELECT id, email FROM campaign_sends WHERE campaign_id = ? "
                    "AND step = 0 AND state = 'pending'", (campaign_id,),
                ).fetchall():
                    if email not in keep:
                        conn.execute("DELETE FROM campaign_sends WHERE id = ?", (send_id,))
                conn.executemany(
                    "INSERT OR IGNORE INTO campaign_sends (campaign_id, email) "
                    "VALUES (?, ?)",
                    [(campaign_id, email) for email in desired],
                )

            if followups is not None:
                conn.execute("DELETE FROM campaign_followups WHERE campaign_id = ?", (campaign_id,))
                conn.executemany(
                    "INSERT INTO campaign_followups "
                    "(campaign_id, step, after_days, subject, body) VALUES (?, ?, ?, ?, ?)",
                    [(campaign_id, step, int(fu["after_days"]), fu["subject"], fu["body"])
                     for step, fu in enumerate(followups, 1)],
                )
                conn.execute(
                    "DELETE FROM campaign_sends WHERE campaign_id = ? "
                    "AND state = 'pending' AND step > ?",
                    (campaign_id, len(followups)),
                )
                for step, fu in enumerate(followups, 1):
                    predecessors = conn.execute(
                        "SELECT p.email, p.sent_at, queued.id FROM campaign_sends p "
                        "LEFT JOIN campaign_sends queued ON queued.campaign_id = p.campaign_id "
                        "AND queued.email = p.email AND queued.step = ? "
                        "WHERE p.campaign_id = ? AND p.step = ? AND p.state = 'sent' "
                        "AND NOT EXISTS (SELECT 1 FROM campaign_replies r "
                        "WHERE r.campaign_id = p.campaign_id AND r.email = p.email)",
                        (step, campaign_id, step - 1),
                    ).fetchall()
                    for email, sent_at, queued_id in predecessors:
                        due = (datetime.fromisoformat(sent_at) + timedelta(
                            days=int(fu["after_days"]))).isoformat()
                        if queued_id is None:
                            conn.execute(
                                "INSERT INTO campaign_sends "
                                "(campaign_id, email, step, not_before) VALUES (?, ?, ?, ?)",
                                (campaign_id, email, step, due),
                            )
                        else:
                            conn.execute(
                                "UPDATE campaign_sends SET not_before = ? "
                                "WHERE id = ? AND state = 'pending'",
                                (due, queued_id),
                            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def set_status(self, campaign_id: int, *, status: str,
                   paused_reason: str = "", resume_at: str = "") -> bool:
        if status not in CAMPAIGN_STATUSES:
            raise ValueError(f"invalid campaign status: {status}")
        conn = self._conn()
        cur = conn.execute(
            "UPDATE campaigns SET status = ?, paused_reason = ?, resume_at = ?, "
            "early_resume_date = CASE WHEN ? = 'user' THEN '' ELSE early_resume_date END, "
            "updated_at = ? WHERE id = ?",
            (status, paused_reason, resume_at, paused_reason, _now(), campaign_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def continue_now(self, campaign_id: int, user_id: str,
                     pakistan_day: str) -> bool:
        """Override today's start clock only; preserve tomorrow's schedule."""
        conn = self._conn()
        cur = conn.execute(
            "UPDATE campaigns SET status = 'running', paused_reason = '', "
            "resume_at = '', early_resume_date = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND "
            "(status IN ('scheduled', 'running') OR "
            "(status = 'paused' AND paused_reason = 'user'))",
            (pakistan_day, _now(), campaign_id, user_id),
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
            f"AND paused_reason = 'account' AND id IN "
            f"(SELECT campaign_id FROM campaign_accounts WHERE account_id IN ({marks}))",
            [_now(), *account_ids],
        )
        conn.commit()
        conn.close()
        return cur.rowcount

    def account_paused_accounts(self) -> list[tuple[int, str]]:
        """DISTINCT (account, user) of every account on an account-paused
        campaign — the scheduler health-checks these to auto-resume."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT DISTINCT ca.account_id, c.user_id FROM campaigns c "
            "JOIN campaign_accounts ca ON ca.campaign_id = c.id "
            "WHERE c.status = 'paused' AND c.paused_reason = 'account'"
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
        for table in ("campaign_sends", "campaign_followups",
                      "campaign_replies", "campaign_accounts",
                      "campaign_hooks", "campaign_bounces", "campaign_drafts"):
            conn.execute(f"DELETE FROM {table} WHERE campaign_id = ?",
                         (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
        conn.commit()
        conn.close()
        return True

    # -- Send outcomes ------------------------------------------------------

    def mark_sent(self, send_id: int, *, subject: str, sent_at: str,
                  account_id: int = 0, body: str = "") -> None:
        conn = self._conn()
        row = conn.execute(
            "SELECT c.user_id, lower(s.email), s.campaign_id, s.step "
            "FROM campaign_sends s JOIN campaigns c ON c.id=s.campaign_id "
            "WHERE s.id=?", (send_id,),
        ).fetchone()
        conn.execute(
            "UPDATE campaign_sends SET state = 'sent', subject = ?, "
            "sent_at = ?, account_id = ?, error = '' WHERE id = ?",
            (subject, sent_at, int(account_id), send_id),
        )
        if row is not None and body:
            conn.execute(
                "INSERT OR IGNORE INTO campaign_drafts "
                "(campaign_id, email, step, subject, body) VALUES (?, ?, ?, ?, ?)",
                (row[2], row[1], row[3], subject, body),
            )
        if row is not None and row[3] == 0:
            conn.execute(
                "INSERT OR IGNORE INTO campaign_recipient_history "
                "(user_id,email,campaign_id,account_id,sent_at) "
                "VALUES (?,?,?,?,?)",
                (row[0], row[1], row[2], int(account_id), sent_at),
            )
        conn.commit()
        conn.close()

    # -- AI opening lines (Phase E5) ---------------------------------------

    def get_draft(self, campaign_id: int, email: str,
                  step: int) -> dict[str, str] | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT subject, body FROM campaign_drafts WHERE "
            "campaign_id = ? AND email = ? AND step = ?",
            (campaign_id, email, step),
        ).fetchone()
        conn.close()
        return {"subject": row[0], "body": row[1]} if row else None

    def set_draft(self, campaign_id: int, email: str, step: int,
                  subject: str, body: str) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO campaign_drafts "
            "(campaign_id, email, step, subject, body) VALUES (?, ?, ?, ?, ?)",
            (campaign_id, email, step, subject, body),
        )
        conn.commit()
        conn.close()

    def get_hook(self, campaign_id: int, email: str) -> str | None:
        """The cached AI opening line for one lead. None = never generated
        (generate it); '' = generated and honestly empty (don't re-ask)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT hook FROM campaign_hooks "
            "WHERE campaign_id = ? AND email = ?",
            (campaign_id, email),
        ).fetchone()
        conn.close()
        return None if row is None else (row[0] or "")

    def set_hook(self, campaign_id: int, email: str, hook: str) -> None:
        """Cache the generated opening line (INSERT OR IGNORE — first
        generation wins, retries never re-call the AI)."""
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO campaign_hooks "
            "(campaign_id, email, hook) VALUES (?, ?, ?)",
            (campaign_id, email, hook or ""),
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
        """No pending sends left -> completed (failed/skipped ones don't
        block it)."""
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


def _empty_counts() -> dict[str, int]:
    """The zeroed progress shape for _counts' setdefault calls."""
    return {"pending": 0, "sent": 0, "failed": 0, "skipped": 0, "replied": 0}


# Module-level singleton (the deps._user_store pattern — tests override it).
_store: CampaignStore | None = None


def get_campaign_store() -> CampaignStore:
    global _store
    if _store is None:
        _store = CampaignStore()
    return _store
