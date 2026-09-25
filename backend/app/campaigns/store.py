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
from typing import Any

from app.auth.models import _now
from app.core.db_paths import operational_db_path
from app.campaigns.tracking import is_repeat_view, is_sender_selfcheck

_INIT_LOCK = threading.RLock()

CAMPAIGN_STATUSES = ("scheduled", "running", "paused", "completed")

#: campaign_sends.state values. 'skipped' = a follow-up dropped because the
#: lead replied (never an error — it is the system working as designed).
SEND_STATES = ("pending", "sent", "failed", "skipped")


def _campaign_tenant(
    conn: sqlite3.Connection, tenant_id: str | None,
) -> str | None:
    """Require explicit scope in tenant mode, or the sole legacy tenant."""
    if tenant_id is not None and not tenant_id.strip():
        raise ValueError("tenant_id must not be blank")
    if os.environ.get("LEADHUNTER_MULTI_TENANT_ENABLED") == "1" and tenant_id is None:
        raise ValueError("tenant_id is required in multi-tenant mode")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)")}
    if "tenant_id" not in columns:
        if tenant_id is not None:
            raise ValueError("campaigns is not tenant-ready")
        return None
    if tenant_id is not None:
        if conn.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone() is None:
            raise ValueError("unknown tenant_id for campaign")
        return tenant_id
    tenants = conn.execute("SELECT id FROM tenants LIMIT 2").fetchall()
    if len(tenants) != 1:
        raise ValueError("ambiguous legacy tenant for campaign")
    return str(tenants[0][0])


class CampaignStore:
    """SQLite persistence for campaigns and their send queue."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = operational_db_path(os.path.join(
                os.path.dirname(__file__), "..", "..", "output", "campaigns.db"
            ))
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
            # E5 additive migrations (plain ALTERs — no rebuild needed).
            cols = [r[1] for r in conn.execute("PRAGMA table_info(campaigns)")]
            if "ai_personalize" not in cols:
                conn.execute("ALTER TABLE campaigns "
                             "ADD COLUMN ai_personalize INTEGER NOT NULL DEFAULT 0")
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
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """One campaign + its pending step-0 send queue (insertion order =
        send order) + its follow-up definitions (steps 1..N). Duplicate
        emails within the list are de-duplicated here. ``account_ids`` adds
        EXTRA sending accounts beyond the primary (E5 multi-account); the
        primary always comes first and duplicates drop."""
        seen: list[str] = []
        for e in emails:
            if e and e not in seen:
                seen.append(e)
        accounts = [account_id]
        for a in account_ids or []:
            if a and a not in accounts:
                accounts.append(int(a))
        now = _now()
        conn = self._conn()
        tenant_id = _campaign_tenant(conn, tenant_id)
        tenant_column = ", tenant_id" if tenant_id is not None else ""
        tenant_value = ", ?" if tenant_id is not None else ""
        params = (user_id, account_id, name, subject, body, start_at,
                  int(daily_limit), int(delay_min_s), int(delay_max_s),
                  1 if ai_personalize else 0, now, now)
        if tenant_id is not None:
            params += (tenant_id,)
        cur = conn.execute(
            "INSERT INTO campaigns (user_id, account_id, name, subject, body, "
            "status, start_at, daily_limit, delay_min_s, delay_max_s, "
            "ai_personalize, created_at, updated_at" + tenant_column + ") "
            "VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?, ?" + tenant_value + ")",
            params,
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
        return self.get(campaign_id, user_id, tenant_id=tenant_id) or {}

    # -- Read -----------------------------------------------------------

    _CAMPAIGN_COLS = ("id, user_id, account_id, name, subject, body, status, "
                      "paused_reason, resume_at, start_at, daily_limit, "
                      "delay_min_s, delay_max_s, ai_personalize, "
                      "created_at, updated_at")

    def get(self, campaign_id: int, user_id: str,
            tenant_id: str | None = None) -> dict[str, Any] | None:
        """One campaign (None when missing or not the caller's) + progress."""
        conn = self._conn()
        tenant_id = _campaign_tenant(conn, tenant_id)
        clause = " AND tenant_id = ?" if tenant_id is not None else ""
        row = conn.execute(
            f"SELECT {self._CAMPAIGN_COLS} FROM campaigns "
            f"WHERE id = ? AND user_id = ?" + clause,
            (campaign_id, user_id, tenant_id) if tenant_id is not None
            else (campaign_id, user_id),
        ).fetchone()
        counts = self._counts(conn, [campaign_id]).get(campaign_id, {})
        conn.close()
        if row is None:
            return None
        out = self._row_to_campaign(row, counts)
        out["account_ids"] = self.campaign_accounts(campaign_id)
        return out

    def list_for_user(self, user_id: str,
                      tenant_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._conn()
        tenant_id = _campaign_tenant(conn, tenant_id)
        clause = " AND tenant_id = ?" if tenant_id is not None else ""
        rows = conn.execute(
            f"SELECT {self._CAMPAIGN_COLS} FROM campaigns "
            f"WHERE user_id = ?" + clause + " ORDER BY id DESC",
            (user_id, tenant_id) if tenant_id is not None else (user_id,),
        ).fetchall()
        counts = self._counts(conn, [r[0] for r in rows])
        conn.close()
        out = [self._row_to_campaign(r, counts.get(r[0], {})) for r in rows]
        for c in out:
            c["account_ids"] = self.campaign_accounts(c["id"])
        return out

    def sends(self, campaign_id: int, user_id: str,
              limit: int = 200,
              tenant_id: str | None = None) -> list[dict[str, Any]] | None:
        """The send queue (pending order first, then sent/failed/skipped).
        None when the campaign is not the caller's."""
        conn = self._conn()
        tenant_id = _campaign_tenant(conn, tenant_id)
        clause = " AND tenant_id = ?" if tenant_id is not None else ""
        owns = conn.execute(
            "SELECT 1 FROM campaigns WHERE id = ? AND user_id = ?" + clause,
            (campaign_id, user_id, tenant_id) if tenant_id is not None
            else (campaign_id, user_id),
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

    def followups(self, campaign_id: int,
                  tenant_id: str | None = None) -> list[dict[str, Any]]:
        """The campaign's follow-up definitions, in step order."""
        conn = self._conn()
        tenant_id = _campaign_tenant(conn, tenant_id)
        clause = " AND c.tenant_id = ?" if tenant_id is not None else ""
        rows = conn.execute(
            "SELECT f.step, f.after_days, f.subject, f.body "
            "FROM campaign_followups f JOIN campaigns c ON c.id = f.campaign_id "
            "WHERE f.campaign_id = ?" + clause + " ORDER BY f.step ASC",
            (campaign_id, tenant_id) if tenant_id is not None else (campaign_id,),
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
                     now_iso: str = "") -> dict[str, Any] | None:
        """The next send due: oldest pending row whose not_before (if any)
        has passed. An empty ``now_iso`` ignores not_before entirely (the
        E3 store-call shape — used by tests/admin inspection)."""
        where = "state = 'pending'"
        params: list[Any] = [campaign_id]
        if now_iso:
            where += " AND (not_before = '' OR not_before <= ?)"
            params.append(now_iso)
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
        """Sent count for the ACCOUNT (all campaigns) on one UTC date — the
        daily cap is per Gmail account, not per campaign. Pre-E5 rows (no
        send-level account) count via the campaign's primary account."""
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM campaign_sends s JOIN campaigns c "
            "ON s.campaign_id = c.id WHERE s.state = 'sent' AND "
            "(CASE WHEN s.account_id > 0 THEN s.account_id ELSE c.account_id END) = ? "
            "AND substr(s.sent_at, 1, 10) = ?",
            (account_id, today),
        ).fetchone()
        conn.close()
        return row[0] if row else 0

    def already_sent_emails(self, user_id: str,
                            emails: list[str],
                            tenant_id: str | None = None) -> set[str]:
        """Which of these emails were ALREADY sent to (any campaign, this
        user)? Used at create time to never double-email a lead."""
        conn = self._conn()
        tenant_id = _campaign_tenant(conn, tenant_id)
        if not emails:
            conn.close()
            return set()
        marks = ",".join("?" * len(emails))
        clause = " AND c.tenant_id = ?" if tenant_id is not None else ""
        rows = conn.execute(
            "SELECT DISTINCT s.email FROM campaign_sends s "
            "JOIN campaigns c ON s.campaign_id = c.id "
            "WHERE c.user_id = ?" + clause +
            f" AND s.state = 'sent' AND s.email IN ({marks})",
            [user_id, *([tenant_id] if tenant_id is not None else []), *emails],
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}

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
                        tenant_id: str | None = None) -> bool:
        """Edit the pitch of an existing campaign (name/subject/body).
        Applies to every send that has NOT gone out yet — already-sent
        rows keep the subject they were sent with (their own record)."""
        conn = self._conn()
        tenant_id = _campaign_tenant(conn, tenant_id)
        clause = " AND tenant_id = ?" if tenant_id is not None else ""
        cur = conn.execute(
            "UPDATE campaigns SET name = ?, subject = ?, body = ?, "
            "updated_at = ? WHERE id = ? AND user_id = ?" + clause,
            (name, subject, body, _now(), campaign_id, user_id, tenant_id)
            if tenant_id is not None else
            (name, subject, body, _now(), campaign_id, user_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def set_status(self, campaign_id: int, *, status: str,
                   paused_reason: str = "", resume_at: str = "",
                   tenant_id: str | None = None) -> bool:
        if status not in CAMPAIGN_STATUSES:
            raise ValueError(f"invalid campaign status: {status}")
        conn = self._conn()
        tenant_id = _campaign_tenant(conn, tenant_id)
        clause = " AND tenant_id = ?" if tenant_id is not None else ""
        cur = conn.execute(
            "UPDATE campaigns SET status = ?, paused_reason = ?, resume_at = ?, "
            "updated_at = ? WHERE id = ?" + clause,
            (status, paused_reason, resume_at, _now(), campaign_id, tenant_id)
            if tenant_id is not None else
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

    def delete(self, campaign_id: int, user_id: str,
               tenant_id: str | None = None) -> bool:
        conn = self._conn()
        tenant_id = _campaign_tenant(conn, tenant_id)
        clause = " AND tenant_id = ?" if tenant_id is not None else ""
        owns = conn.execute(
            "SELECT 1 FROM campaigns WHERE id = ? AND user_id = ?" + clause,
            (campaign_id, user_id, tenant_id) if tenant_id is not None
            else (campaign_id, user_id),
        ).fetchone()
        if owns is None:
            conn.close()
            return False
        for table in ("campaign_sends", "campaign_followups",
                      "campaign_replies", "campaign_accounts",
                      "campaign_hooks"):
            conn.execute(f"DELETE FROM {table} WHERE campaign_id = ?",
                         (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
        conn.commit()
        conn.close()
        return True

    # -- Send outcomes ------------------------------------------------------

    def mark_sent(self, send_id: int, *, subject: str, sent_at: str,
                  account_id: int = 0) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE campaign_sends SET state = 'sent', subject = ?, "
            "sent_at = ?, account_id = ?, error = '' WHERE id = ?",
            (subject, sent_at, int(account_id), send_id),
        )
        conn.commit()
        conn.close()

    # -- AI opening lines (Phase E5) ---------------------------------------

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
