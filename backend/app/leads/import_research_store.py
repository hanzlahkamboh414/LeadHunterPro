"""Durable per-user file research jobs and relevant-only daily accounting."""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone

from app.lead_research.service import _email_hash

DEFAULT_DAILY_LIMIT = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class ImportResearchStore:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or os.path.join(
            os.path.dirname(__file__), "..", "..", "output", "import_research.db")
        os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)
        conn = self._conn()
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS import_jobs (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                name TEXT NOT NULL, filename TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                rejected INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_import_jobs_user
                ON import_jobs(user_id, created_at);
            CREATE TABLE IF NOT EXISTS import_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL, email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(job_id, email)
            );
            CREATE INDEX IF NOT EXISTS ix_import_rows_status
                ON import_rows(status, id);
            CREATE TABLE IF NOT EXISTS import_limits (
                user_id TEXT PRIMARY KEY, daily_limit INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS import_daily_claims (
                user_id TEXT NOT NULL, day_utc TEXT NOT NULL,
                email_hash TEXT NOT NULL, job_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('reserved', 'counted')),
                PRIMARY KEY(user_id, day_utc, email_hash)
            );
            CREATE INDEX IF NOT EXISTS ix_import_claims_usage
                ON import_daily_claims(user_id, day_utc, status);
        """)
        conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        return conn

    def create_job(self, user_id: str, filename: str, emails: list[str],
                   rejected: int = 0, name: str = "") -> dict:
        if not user_id or not emails:
            raise ValueError("User and at least one email are required")
        job_id = uuid.uuid4().hex[:16]
        title = (name or filename or "Uploaded leads").strip()[:100]
        stamp = _now()
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO import_jobs(id,user_id,name,filename,rejected,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, user_id, title, filename[:180], rejected, stamp, stamp),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO import_rows(job_id,email) VALUES (?,?)",
                [(job_id, email.lower()) for email in emails],
            )
        conn.close()
        return self.get_job(job_id, user_id)

    def _job_out(self, row: sqlite3.Row, conn: sqlite3.Connection) -> dict:
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT status, count(*) FROM import_rows WHERE job_id=? GROUP BY status",
            (row["id"],),
        )}
        return {
            "id": row["id"], "user_id": row["user_id"], "name": row["name"],
            "filename": row["filename"], "status": row["status"],
            "total": sum(counts.values()), "rejected": row["rejected"],
            "relevant": counts.get("relevant", 0) + counts.get("existing", 0),
            "irrelevant": counts.get("irrelevant", 0),
            "failed": counts.get("failed", 0),
            "pending": counts.get("pending", 0) + counts.get("working", 0)
            + counts.get("cancelled", 0),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def get_job(self, job_id: str, user_id: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM import_jobs WHERE id=? AND user_id=?",
            (job_id, user_id),
        ).fetchone()
        out = self._job_out(row, conn) if row else None
        conn.close()
        return out

    def list_jobs(self, user_id: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM import_jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
            (user_id,),
        ).fetchall()
        out = [self._job_out(row, conn) for row in rows]
        conn.close()
        return out

    def collection_emails(self, job_id: str, user_id: str) -> set[str] | None:
        conn = self._conn()
        owner = conn.execute(
            "SELECT 1 FROM import_jobs WHERE id=? AND user_id=?",
            (job_id, user_id),
        ).fetchone()
        if not owner:
            conn.close()
            return None
        rows = conn.execute(
            "SELECT email FROM import_rows WHERE job_id=? "
            "AND status IN ('relevant','existing')", (job_id,),
        ).fetchall()
        conn.close()
        return {row[0] for row in rows}

    def recent_activity(self, job_id: str, user_id: str, limit: int = 30) -> list[dict] | None:
        """Recent processed addresses for the owner's live research view."""
        conn = self._conn()
        owner = conn.execute(
            "SELECT 1 FROM import_jobs WHERE id=? AND user_id=?", (job_id, user_id)
        ).fetchone()
        if not owner:
            conn.close()
            return None
        rows = conn.execute(
            "SELECT email,status,reason FROM import_rows WHERE job_id=? "
            "AND status IN ('working','relevant','existing','irrelevant','failed') "
            "ORDER BY id DESC LIMIT ?", (job_id, max(1, min(limit, 100))),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def rename_job(self, job_id: str, user_id: str, name: str) -> bool:
        name = name.strip()[:100]
        if not name:
            raise ValueError("Name cannot be empty")
        conn = self._conn()
        with conn:
            result = conn.execute(
                "UPDATE import_jobs SET name=?,updated_at=? WHERE id=? AND user_id=?",
                (name, _now(), job_id, user_id),
            )
        conn.close()
        return result.rowcount > 0

    def cancel_job(self, job_id: str, user_id: str) -> bool:
        conn = self._conn()
        with conn:
            result = conn.execute(
                "UPDATE import_jobs SET status='cancelled',updated_at=? "
                "WHERE id=? AND user_id=? AND status NOT IN ('completed','cancelled')",
                (_now(), job_id, user_id),
            )
            if result.rowcount:
                conn.execute(
                    "UPDATE import_rows SET status='cancelled' "
                    "WHERE job_id=? AND status='pending'", (job_id,),
                )
        conn.close()
        return result.rowcount > 0

    def pause_job(self, job_id: str, user_id: str) -> bool:
        """Stop taking new rows; an address already in research may finish."""
        conn = self._conn()
        with conn:
            result = conn.execute(
                "UPDATE import_jobs SET status='paused',updated_at=? "
                "WHERE id=? AND user_id=? "
                "AND status IN ('queued','running','waiting_quota')",
                (_now(), job_id, user_id),
            )
        conn.close()
        return result.rowcount > 0

    def resume_job(self, job_id: str, user_id: str) -> bool:
        """Continue a paused or cancelled upload without redoing finished rows."""
        conn = self._conn()
        with conn:
            row = conn.execute(
                "SELECT status FROM import_jobs WHERE id=? AND user_id=?",
                (job_id, user_id),
            ).fetchone()
            if not row or row[0] not in ("paused", "cancelled"):
                resumed = False
            else:
                if row[0] == "cancelled":
                    conn.execute(
                        "UPDATE import_rows SET status='pending',reason='' "
                        "WHERE job_id=? AND status='cancelled'", (job_id,),
                    )
                remaining = conn.execute(
                    "SELECT 1 FROM import_rows WHERE job_id=? "
                    "AND status IN ('pending','working') LIMIT 1", (job_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE import_jobs SET status=?,updated_at=? WHERE id=?",
                    ("queued" if remaining else "completed", _now(), job_id),
                )
                resumed = True
        conn.close()
        return resumed

    def delete_collection(self, job_id: str, user_id: str) -> bool:
        conn = self._conn()
        with conn:
            row = conn.execute(
                "SELECT status FROM import_jobs WHERE id=? AND user_id=?",
                (job_id, user_id),
            ).fetchone()
            working = conn.execute(
                "SELECT 1 FROM import_rows WHERE job_id=? AND status='working'",
                (job_id,),
            ).fetchone()
            allowed = row is not None and row[0] in ("completed", "cancelled") and not working
            if allowed:
                conn.execute("DELETE FROM import_rows WHERE job_id=?", (job_id,))
                conn.execute("DELETE FROM import_jobs WHERE id=?", (job_id,))
        conn.close()
        return allowed

    def next_rows(self, limit: int = 50) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id,job_id,email,attempts,user_id FROM ("
            "SELECT r.id,r.job_id,r.email,r.attempts,j.user_id,"
            "ROW_NUMBER() OVER (PARTITION BY j.user_id ORDER BY r.id) AS rn "
            "FROM import_rows r JOIN import_jobs j ON j.id=r.job_id "
            "WHERE r.status='pending' AND j.status IN ('queued','running','waiting_quota')"
            ") WHERE rn=1 ORDER BY id LIMIT ?", (limit,),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def is_cancelled(self, job_id: str) -> bool:
        conn = self._conn()
        row = conn.execute("SELECT status FROM import_jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        return row is None or row[0] == "cancelled"

    def mark_working(self, row_id: int, job_id: str) -> bool:
        conn = self._conn()
        with conn:
            result = conn.execute(
                "UPDATE import_rows SET status='working',attempts=attempts+1 "
                "WHERE id=? AND job_id=? AND status='pending' "
                "AND EXISTS (SELECT 1 FROM import_jobs WHERE id=? "
                "AND status IN ('queued','running','waiting_quota'))",
                (row_id, job_id, job_id),
            )
            if result.rowcount:
                conn.execute(
                    "UPDATE import_jobs SET status='running',updated_at=? "
                    "WHERE id=?", (_now(), job_id),
                )
        conn.close()
        return result.rowcount > 0

    def mark_waiting_quota(self, job_id: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE import_jobs SET status='waiting_quota',updated_at=? "
                "WHERE id=? AND status!='cancelled'", (_now(), job_id),
            )
        conn.close()

    def daily_limit(self, user_id: str, *, is_owner: bool = False) -> int | None:
        if is_owner:
            return None
        conn = self._conn()
        row = conn.execute(
            "SELECT daily_limit FROM import_limits WHERE user_id=?", (user_id,),
        ).fetchone()
        conn.close()
        return int(row[0]) if row else DEFAULT_DAILY_LIMIT

    def set_daily_limit(self, user_id: str, limit: int) -> None:
        if not 0 <= limit <= 1_000_000:
            raise ValueError("Daily limit must be 0–1,000,000")
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO import_limits(user_id,daily_limit) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET daily_limit=excluded.daily_limit",
                (user_id, limit),
            )
        conn.close()

    def daily_usage(self, user_id: str, day: str | None = None) -> int:
        conn = self._conn()
        row = conn.execute(
            "SELECT count(*) FROM import_daily_claims WHERE user_id=? "
            "AND day_utc=? AND status='counted'", (user_id, day or _today()),
        ).fetchone()
        conn.close()
        return int(row[0])

    def reserve(self, user_id: str, email: str, job_id: str,
                day: str | None = None, *, is_owner: bool = False) -> str:
        """Atomically reserve capacity; irrelevant results release it later."""
        day = day or _today()
        eh = _email_hash(email)
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status FROM import_daily_claims "
                "WHERE user_id=? AND day_utc=? AND email_hash=?",
                (user_id, day, eh),
            ).fetchone()
            if existing:
                conn.commit()
                return existing[0]
            row = conn.execute(
                "SELECT daily_limit FROM import_limits WHERE user_id=?", (user_id,),
            ).fetchone()
            limit = int(row[0]) if row else DEFAULT_DAILY_LIMIT
            used = conn.execute(
                "SELECT count(*) FROM import_daily_claims WHERE user_id=? AND day_utc=?",
                (user_id, day),
            ).fetchone()[0]
            if not is_owner and used >= limit:
                conn.commit()
                return "full"
            conn.execute(
                "INSERT INTO import_daily_claims(user_id,day_utc,email_hash,job_id,status) "
                "VALUES (?,?,?,?, 'reserved')", (user_id, day, eh, job_id),
            )
            conn.commit()
            return "reserved"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_status(self, user_id: str, email: str,
                     day: str | None = None) -> str | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT status FROM import_daily_claims "
            "WHERE user_id=? AND day_utc=? AND email_hash=?",
            (user_id, day or _today(), _email_hash(email)),
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def finish_row(self, row_id: int, job_id: str, status: str,
                   reason: str = "", *, user_id: str = "", email: str = "",
                   claim_day: str = "") -> None:
        if status not in {"relevant", "existing", "irrelevant", "failed", "pending", "cancelled"}:
            raise ValueError("Bad import row status")
        conn = self._conn()
        with conn:
            if user_id and email and claim_day:
                if status == "relevant":
                    result = conn.execute(
                        "UPDATE import_daily_claims SET status='counted' "
                        "WHERE user_id=? AND day_utc=? AND email_hash=? "
                        "AND status IN ('reserved','counted')",
                        (user_id, claim_day, _email_hash(email)),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("Relevant lead has no reserved daily slot")
                elif status in {"irrelevant", "failed", "cancelled"}:
                    conn.execute(
                        "DELETE FROM import_daily_claims WHERE user_id=? AND day_utc=? "
                        "AND email_hash=? AND status='reserved'",
                        (user_id, claim_day, _email_hash(email)),
                    )
            conn.execute(
                "UPDATE import_rows SET status=?,reason=? WHERE id=?",
                (status, reason[:200], row_id),
            )
            left = conn.execute(
                "SELECT count(*) FROM import_rows WHERE job_id=? "
                "AND status IN ('pending','working')", (job_id,),
            ).fetchone()[0]
            if left == 0:
                conn.execute(
                    "UPDATE import_jobs SET status='completed',updated_at=? "
                    "WHERE id=? AND status!='cancelled'", (_now(), job_id),
                )
            else:
                conn.execute(
                    "UPDATE import_jobs SET updated_at=? WHERE id=?", (_now(), job_id),
                )
        conn.close()

    def retry_row(self, row_id: int, reason: str) -> None:
        """Return a crashed row to the queue without losing its quota claim."""
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE import_rows SET status='pending',reason=? WHERE id=? "
                "AND status='working'", (reason[:200], row_id),
            )
        conn.close()

    def recover_interrupted(self) -> int:
        conn = self._conn()
        with conn:
            result = conn.execute(
                "UPDATE import_rows SET status='pending' WHERE status='working'")
            conn.execute(
                "UPDATE import_jobs SET status='queued',updated_at=? "
                "WHERE status='running'", (_now(),),
            )
            conn.execute(
                "DELETE FROM import_daily_claims WHERE status='reserved' AND day_utc<?",
                (_today(),),
            )
        conn.close()
        return result.rowcount
