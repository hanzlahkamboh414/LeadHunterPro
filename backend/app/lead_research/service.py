"""AI Lead Research — Service (store + agent + persist).

Ties the AILeadResearchAgent into a service that can:
- research(email, domain) → LeadDossier + persist to SQLite
- get(email) → stored LeadDossier or None
- list_leads() → all stored dossiers
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

from app.lead_research.agent import AILeadResearchAgent, DEAD_DOMAIN_MARKER
from app.lead_research.models import CRM_STATUSES, LeadDossier, LeadMeta

logger = logging.getLogger(__name__)


def _email_hash(email: str) -> str:
    """Deterministic hash for email key."""
    import hashlib
    return hashlib.sha256(email.lower().strip().encode()).hexdigest()[:16]


def is_dead_domain_dossier(dossier: LeadDossier) -> bool:
    """True when a dossier was rejected at the dead-domain MX gate.

    Uses the agent's :data:`DEAD_DOMAIN_MARKER` so pipeline, list and cleanup
    all agree on the SAME definition — one source of truth, no magic strings.
    """
    fit = getattr(dossier, "fit", "") or ""
    return (
        getattr(dossier, "recommendation", "") == "skip"
        and DEAD_DOMAIN_MARKER in fit
    )


#: Reason-substrings that mark a delete as a USER REJECTION of the company —
#: the only deletes that feed identity-rejection learning (Phase E). "junk" and
#: "manual" are NOT here on purpose: the Junk sweep is a data-hygiene action,
#: not a verdict on the company itself, and a plain manual delete carries no
#: rejection signal. Underscores/dashes are folded to spaces for matching, so
#: ``irrelevant-2026-09-08: Cloud CM SaaS`` and ``reason="non_client"`` both hit.
_REJECTION_REASON_MARKS = ("irrelevant", "not our client", "non client", "nonclient")


def _is_rejection_reason(reason: str) -> bool:
    """True when ``reason`` carries an explicit "not our client" verdict."""
    r = (reason or "").lower().replace("_", " ").replace("-", " ")
    return any(m in r for m in _REJECTION_REASON_MARKS)


#: Serializes store construction across the process's worker threads. Every
#: job worker builds a LeadResearchStore / PendingLeadsStore on the SAME DB
#: file, and ``_init_db`` runs check-then-ALTER migrations: two concurrent
#: constructions both see a column missing, both ALTER, and the loser dies
#: with ``duplicate column name`` (the measured Phase 4 admission-control
#: flake — a queued job failed at construction instead of running). The
#: folders REBUILD (rename → create → copy → drop) cannot be made
#: race-tolerant with a catch, so the lock is the primary fix;
#: :func:`_add_column`'s duplicate-column tolerance is defense-in-depth.
_INIT_DB_LOCK = threading.RLock()


def _add_column(conn: sqlite3.Connection, table: str, name: str, decl: str) -> None:
    """Guarded additive ALTER — the same pattern JobStore._init_db uses.

    TOCTOU-safe: a racing construction may have added the column between
    our PRAGMA check and our ALTER; "duplicate column name" then means the
    migration already happened, not a failure. Any OTHER OperationalError
    still raises — swallowing a real lock/schema error would leave the
    schema silently missing a column.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc):
            raise


class LeadResearchStore:
    """SQLite store for LeadDossier results."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            import os
            db_path = os.path.join(os.path.dirname(__file__), "..", "..", "output", "lead_research.db")
        self._db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        """Open a SQLite connection that WAITS under lock/IO contention.

        Galti #1 fix: consumers crashed with ``sqlite3.OperationalError: disk I/O
        error`` because rows were claimed without a busy_timeout. Every other
        store (yield/candidate) sets ``PRAGMA busy_timeout``; this one did not,
        so a write under lock contention failed the worker. WAL is a persistent
        DB-header property (inherited per-connection), so a fresh connection here
        still gets WAL; only the busy-wait must be re-applied per connection.
        """
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        # Serialized (see _INIT_DB_LOCK): under 10-user load several workers
        # construct this store at once and their migrations must not race.
        with _INIT_DB_LOCK:
            self._init_db_serialized()

    def _init_db_serialized(self) -> None:
        import os
        import time as _time
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = self._conn()
        # WAL (permanent, §7 fix for "searching never started"): in the default
        # rollback journal a writer locks the WHOLE database, so while the
        # background discovery worker writes a pass every reader — GET /jobs,
        # GET /leads — the frontend's live status poll — blocks up to
        # busy_timeout and the run reads as stuck at "0 results". WAL lets a
        # reader ALWAYS serve the last committed snapshot, so a discovery
        # writer never blocks the frontend. journal_mode persists in the DB
        # header the first time it is set (a later set is a no-op).
        #
        # The pragma needs a momentary exclusive lock and does NOT always
        # invoke the busy handler, so under concurrent store constructions
        # (every job worker builds a LeadResearchStore on the same file) it
        # can surface SQLITE_BUSY outright. Retry — whoever wins the race
        # sets the persistent header and everyone else's pragma is a no-op.
        for _attempt in range(5):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError:
                if _attempt == 4:
                    raise
                _time.sleep(0.2)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dossiers (
                email_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                domain TEXT NOT NULL,
                dossier_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Additive migration — user organization metadata (Phase B). Existing
        # live DBs predate these columns, so add them guarded (never touch
        # dossier_json). `save()`'s upsert only updates dossier_json+updated_at
        # on conflict, so these ride along untouched and survive a pipeline
        # re-research (user metadata is never clobbered).
        _add_column(conn, "dossiers", "folder", "TEXT NOT NULL DEFAULT ''")
        _add_column(conn, "dossiers", "tags", "TEXT NOT NULL DEFAULT '[]'")
        # Additive migration — admin visibility flag (Dashboard data control).
        # The admin HIDES a date/search/lead from the USER views; the dossier
        # stays in the DB (reversible via SHOW) and the admin dashboard still
        # sees it. Default 0 keeps every existing caller byte-compatible (old
        # code simply ignores the new column).
        _add_column(conn, "dossiers", "hidden", "INTEGER NOT NULL DEFAULT 0")
        # Additive migration — persisted FILTER columns (Phase 2, frontend
        # speed). The leads list used to materialize EVERY dossier (full JSON)
        # + re-gate it in Python + paginate LAST, so a request scanned the
        # whole store — the root cause of a slow Companies screen.
        # recommendation / potential_score / bound let list_leads filter,
        # sort and paginate IN SQL. They are written at save() (the
        # deterministic gate, never a stale AI-era label) and backfilled
        # once below — same guarded-ALTER pattern as folder/tags/hidden.
        _add_column(conn, "dossiers", "recommendation", "TEXT NOT NULL DEFAULT ''")
        _add_column(conn, "dossiers", "potential_score", "REAL NOT NULL DEFAULT 0")
        _add_column(conn, "dossiers", "bound", "INTEGER NOT NULL DEFAULT 0")
        # Per-user data isolation. Empty string = pre-migration rows (admin
        # sees all).
        _add_column(conn, "dossiers", "user_id", "TEXT NOT NULL DEFAULT ''")
        # Trade routing (big-bang P1): the lead's OWN canonical trade slug
        # (tradefold.normalize_trade of the researched industry string) —
        # written by save(), and lazy-backfilled once below from the
        # evidence pre-P1 rows already carry. '' = honest "trade unknown":
        # such rows are trade-agnostic for serving (fail-open), never a
        # guess forced into one of the 14 buckets.
        _add_column(conn, "dossiers", "trade", "TEXT NOT NULL DEFAULT ''")
        # Phase B.2 — first-class folder catalog. A folder is a persisted, clickable
        # group (empty folders included — "create the folder first, then move leads").
        # Names are also backfilled from dossiers on read, so folders that only ever
        # existed as values on leads self-heal into the catalog (no data loss).
        #
        # Multi-user fix (2026-09-12): the catalog used to key on name ALONE, so a
        # folder one account created showed in EVERY account — data was isolated
        # but its organization was not. The key is now (user_id, name): each
        # account owns its own folder rows, exactly like its dossier rows.
        # user_id='' = pre-auth/legacy rows (visible on the admin's own dashboard,
        # same rule as legacy dossiers).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                user_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, name)
            )
        """)
        # Guarded REBUILD (not a bare ALTER — the primary key itself changes):
        # an existing single-tenant catalog (name PRIMARY KEY, no user_id) is
        # copied into the per-owner table with its rows kept as legacy ('').
        fcols = {row[1] for row in conn.execute("PRAGMA table_info(folders)")}
        if "user_id" not in fcols:
            conn.execute("ALTER TABLE folders RENAME TO folders_old_singleuser")
            conn.execute("""
                CREATE TABLE folders (
                    user_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, name)
                )
            """)
            conn.execute(
                "INSERT INTO folders (user_id, name, created_at) "
                "SELECT '', name, created_at FROM folders_old_singleuser"
            )
            conn.execute("DROP TABLE folders_old_singleuser")
        # Admin audit trail — which emails the user deleted and why (manual vs
        # Junk sweep). Append-only, never purged by a re-research. Admin reads it
        # (GET /admin/deleted); a re-delete updates the timestamp ("deleted
        # again on <this date>"), so the log always reflects the latest state.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deleted_leads (
                email TEXT PRIMARY KEY,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reason TEXT NOT NULL DEFAULT 'manual'
            )
        """)
        # Delete-feedback loop (2026-09-12): WHO deleted (user_id/username —
        # the admin feed names the rejector), the full dossier snapshot
        # (dossier_json — powers admin Restore), and the admin's answer
        # (admin_decision: '' pending / 'confirmed' purge-corroborated /
        # 'restored' back with the user). Legacy rows default to anonymous
        # pending — the historical log stays honest without inventing users.
        _add_column(conn, "deleted_leads", "user_id", "TEXT NOT NULL DEFAULT ''")
        _add_column(conn, "deleted_leads", "username", "TEXT NOT NULL DEFAULT ''")
        _add_column(conn, "deleted_leads", "dossier_json", "TEXT NOT NULL DEFAULT ''")
        _add_column(conn, "deleted_leads", "admin_decision", "TEXT NOT NULL DEFAULT ''")
        # Cross-user lead sharing (Phase 2 demand fix). The dossiers.user_id
        # column keeps the FIRST owner (the run that researched the lead);
        # this junction records EVERY user whose own search surfaced the
        # lead. Why it matters under multi-user load: a cache hit in user
        # B's run must not burn a re-research (credit saver, already true)
        # AND must still show the lead on B's dashboard — the old single
        # column could only name one user, so B's run consumed the shared
        # dossier invisibly. The PK also backs the per-user visibility
        # EXISTS lookups (email_hash-first, the same key shape as dossiers).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dossier_owners (
                email_hash TEXT NOT NULL,
                user_id TEXT NOT NULL,
                PRIMARY KEY (email_hash, user_id)
            )
        """)
        # Backfill: every existing single-owner row stays visible to its
        # owner through the junction too (idempotent — INSERT OR IGNORE).
        conn.execute("""
            INSERT OR IGNORE INTO dossier_owners (email_hash, user_id)
            SELECT email_hash, user_id FROM dossiers WHERE user_id <> ''
        """)
        # CRM pipeline (Phase E1). The lead's pipeline STAGE and next action
        # live in their own columns (same survival rule as folder/tags — the
        # research payload is never touched, so a re-research can't wipe the
        # user's CRM state). Default 'researched' is honest: a stored dossier
        # has BY DEFINITION been through research; 'new' exists for the user
        # to set deliberately (a fresh un-worked lead they're re-tracking).
        _add_column(conn, "dossiers", "crm_status", "TEXT NOT NULL DEFAULT 'researched'")
        _add_column(conn, "dossiers", "next_action", "TEXT NOT NULL DEFAULT ''")
        # The immutable timeline — every stage change, next-action update and
        # note is APPENDED here (never updated, never deleted), so a lead's
        # story is reconstructable and future phases (emails, replies) append
        # the same way. `kind` names the event type; `detail` is human text.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crm_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_hash TEXT NOT NULL,
                email TEXT NOT NULL,
                user_id TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_events_email "
            "ON crm_events (email_hash, id)"
        )
        self._backfill_filter_columns(conn)
        self._backfill_trade_columns(conn)
        conn.commit()
        conn.close()

    def _backfill_trade_columns(self, conn: sqlite3.Connection) -> None:
        """Lazy trade backfill (big-bang P1): pre-P1 dossier rows get their
        canonical trade from the evidence they ALREADY carry — the
        researched ``company.industry`` string, folded through
        ``tradefold.normalize_trade``.

        Idempotence: only ``trade = ''`` rows are scanned; a row whose
        industry folds to '' stays '' (honest unknown) and is simply
        re-scanned on a later boot — a few thousand string folds, not even
        a measurable cost. A corrupt legacy row never blocks boot.
        """
        from app.discovery.tradefold import normalize_trade

        rows = conn.execute(
            "SELECT email_hash, dossier_json FROM dossiers WHERE trade = ''"
        ).fetchall()
        for eh, payload in rows:
            try:
                d = LeadDossier.from_dict(json.loads(payload))
            except (ValueError, TypeError):
                continue  # a corrupt legacy row is not our job to fix here
            trade = normalize_trade(d.company.industry)
            if trade:
                conn.execute(
                    "UPDATE dossiers SET trade = ? WHERE email_hash = ?",
                    (trade, eh),
                )

    def _backfill_filter_columns(self, conn: sqlite3.Connection) -> None:
        """One-time (idempotent) pass that fills the persisted filter columns for
        rows saved BEFORE the columns existed (``recommendation = ''``).

        Applies TODAY'S deterministic gate to every legacy row, so SQL filtering
        in :meth:`query_leads` sees the same verdicts the old read-time re-gate
        produced. Idempotent by construction: a row is only touched while its
        column is still ``''`` — after the first pass nothing matches. A corrupt
        legacy row never blocks boot (skipped, logged by the reader).
        """
        from app.lead_research.scoring import regate_recommendation

        rows = conn.execute(
            "SELECT email_hash, dossier_json FROM dossiers WHERE recommendation = ''"
        ).fetchall()
        for eh, payload in rows:
            try:
                d = LeadDossier.from_dict(json.loads(payload))
            except (ValueError, TypeError):
                continue  # a corrupt legacy row is not our job to fix here
            try:
                rec = regate_recommendation(d)
            except Exception:  # noqa: BLE001 — a broken profile must not block boot
                continue
            conn.execute(
                "UPDATE dossiers SET recommendation = ?, potential_score = ?, bound = ? "
                "WHERE email_hash = ?",
                (rec, float(d.potential_score), 1 if d.person.bound else 0, eh),
            )

    @staticmethod
    def _filter_values(dossier: LeadDossier) -> tuple[str, float, int]:
        """The three persisted filter columns for one dossier.

        ``recommendation`` is TODAY'S deterministic gate (regate), never the stale
        AI-era probe label — the same authority the old read-time list used, so a
        fluxed label never filters as itself. A broken profile/role import must
        never block a save, so it degrades to the stored label (honest fallback).
        """
        from app.lead_research.scoring import regate_recommendation

        try:
            rec = regate_recommendation(dossier)
        except Exception:  # noqa: BLE001
            rec = dossier.recommendation or "skip"
        return rec, float(dossier.potential_score), 1 if dossier.person.bound else 0

    def save(self, dossier: LeadDossier, user_id: str = "") -> None:
        """Save or update a dossier (writes the persisted filter columns too)."""
        from app.discovery.tradefold import normalize_trade

        eh = _email_hash(dossier.email)
        rec, score, bound = self._filter_values(dossier)
        # The lead's OWN trade from its researched industry string (P1) —
        # research evidence, refreshed on every re-research, never the
        # SEARCHED trade of whichever run happened to save the row.
        trade = normalize_trade(dossier.company.industry)
        conn = self._conn()
        conn.execute("""
            INSERT INTO dossiers (email_hash, email, domain, dossier_json,
                                  recommendation, potential_score, bound,
                                  user_id, trade, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(email_hash) DO UPDATE SET
                dossier_json = excluded.dossier_json,
                recommendation = excluded.recommendation,
                potential_score = excluded.potential_score,
                bound = excluded.bound,
                trade = excluded.trade,
                updated_at = CURRENT_TIMESTAMP
        """, (eh, dossier.email, dossier.domain, json.dumps(dossier.to_dict()),
              rec, score, bound, user_id, trade))
        # Sharing (Phase 2): the researcher's own search surfaced this lead —
        # record the owner even when the upsert lands on another user's row
        # (two jobs racing the same email; the column keeps the FIRST owner,
        # the junction keeps BOTH visible). OR IGNORE: re-research of an
        # already-owned lead is a no-op.
        if user_id:
            conn.execute(
                "INSERT OR IGNORE INTO dossier_owners (email_hash, user_id) "
                "VALUES (?, ?)",
                (eh, user_id),
            )
        conn.commit()
        conn.close()

    def add_owner(self, email: str, user_id: str) -> bool:
        """Record that ``user_id``'s own search surfaced an EXISTING dossier.

        The cache-hit path of the pipeline: user B's run found the lead
        already researched (by A), reuses the dossier (no re-research), and
        B must still see the lead on their dashboard. INSERT ... SELECT
        guards existence — an unknown email is an honest False, never a
        dangling owner row. Returns True when a NEW owner row was created.
        """
        if not user_id:
            return False
        eh = _email_hash(email)
        conn = self._conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO dossier_owners (email_hash, user_id) "
            "SELECT email_hash, ? FROM dossiers WHERE email_hash = ?",
            (user_id, eh),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def get(self, email: str) -> LeadDossier | None:
        """Retrieve a dossier by email."""
        eh = _email_hash(email)
        conn = self._conn()
        row = conn.execute(
            "SELECT dossier_json FROM dossiers WHERE email_hash = ?", (eh,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return LeadDossier.from_dict(json.loads(row[0]))

    @staticmethod
    def _shared_visible(alias: str = "d") -> str:
        """The cross-user sharing EXISTS clause (Phase 2).

        A user sees a dossier when THEIR OWN search surfaced it — recorded
        in :data:`dossier_owners` — even when the ``user_id`` column names
        the first researcher. Two users searching overlapping markets share
        the research (the cache hit burns no credits) and each still gets
        the lead on their own dashboard. ``alias`` must name the dossiers
        table in the caller's FROM clause (the outer column must be
        qualified — a bare ``email_hash`` inside the subquery would resolve
        to the OWNERS row and match itself).
        """
        return (
            f"EXISTS (SELECT 1 FROM dossier_owners o "
            f"WHERE o.email_hash = {alias}.email_hash AND o.user_id = ?)"
        )

    def list_all(self, user_id: str = "", is_admin: bool = False,
                 include_legacy: bool = False) -> list[LeadDossier]:
        """List all stored dossiers.

        Per-user isolation: a non-admin only lists their OWN dossiers
        (their user_id OR a dossier_owners row — see :meth:`_shared_visible`);
        ``include_legacy`` adds the pre-auth (``user_id=''``) rows — the admin's
        own-dashboard scope.
        """
        conn = self._conn()
        args: list[Any] = []
        conds = ""
        if user_id and not is_admin:
            shared = self._shared_visible()
            if include_legacy:
                conds = f"WHERE (d.user_id = ? OR d.user_id = '' OR {shared})"
            else:
                conds = f"WHERE (d.user_id = ? OR {shared})"
            args = [user_id, user_id]
        rows = conn.execute(
            f"SELECT d.dossier_json FROM dossiers d {conds} ORDER BY d.updated_at DESC",
            args,
        ).fetchall()
        conn.close()
        return [LeadDossier.from_dict(json.loads(r[0])) for r in rows]

    def count(self) -> int:
        conn = self._conn()
        n = conn.execute("SELECT COUNT(*) FROM dossiers").fetchone()[0]
        conn.close()
        return n

    def delete(self, email: str, *, reason: str = "manual", user_id: str = "",
               username: str = "", is_admin: bool = False) -> bool:
        """Delete one dossier by email; return True if it existed.

        ``reason`` records WHY into the ``deleted_leads`` audit trail the admin
        screen reads ("kon kon c email delete ki") — the user-facing delete
        dialog sends a structured slug (``not_our_client`` / ``bad_data`` /
        ``duplicate`` / ``already_contacted`` / ``low_quality`` / ``other``).
        ``user_id``/``username`` name WHO deleted (the admin feed shows
        "fulane user ne ye lead fulani reason se delete ki"), and the full
        dossier JSON is STASHED so the admin can Restore the lead later.
        Same transaction, so the log can never show a deletion the dossier
        survived (or vice versa).

        When ``reason`` carries a rejection verdict (``not_our_client`` —
        folded to "not our client" by the mark matcher), the dossier's
        company+domain are fed to fit-learning as a USER rejection, WITH the
        corroboration context: ``user_id`` names the rejector (two DISTINCT
        users are needed to purge an identity on user verdicts alone) and the
        rejection is immediately decisive only when the RESEARCH itself
        agreed (the dossier's own grounded "not our client" verdict) or an
        admin made it — the gaming guard against a user casually clicking
        the strong reason to "sirf safai" karne ke liye.
        """
        eh = _email_hash(email)
        conn = self._conn()
        row = conn.execute(
            "SELECT dossier_json FROM dossiers WHERE email_hash = ?", (eh,)
        ).fetchone()
        cur = conn.execute("DELETE FROM dossiers WHERE email_hash = ?", (eh,))
        if cur.rowcount > 0:
            # Purge the sharing junction too — a deleted lead has no owners.
            conn.execute(
                f"DELETE FROM dossier_owners WHERE email_hash = ?", (eh,)
            )
            # CRM timeline goes WITH the row (no orphan events). A restored
            # lead honestly restarts at 'researched' — the old stage died
            # with the delete decision.
            conn.execute("DELETE FROM crm_events WHERE email_hash = ?", (eh,))
            conn.execute(
                "INSERT INTO deleted_leads "
                "(email, deleted_at, reason, user_id, username, dossier_json, "
                " admin_decision) "
                "VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, '') "
                "ON CONFLICT(email) DO UPDATE SET "
                "deleted_at = CURRENT_TIMESTAMP, reason = excluded.reason, "
                "user_id = excluded.user_id, username = excluded.username, "
                "dossier_json = excluded.dossier_json, admin_decision = ''",
                (email, reason or "manual", user_id, username,
                 row[0] if row is not None else ""),
            )
            conn.commit()
            if row is not None and _is_rejection_reason(reason):
                self._feed_user_rejection(
                    LeadDossier.from_dict(json.loads(row[0])),
                    user_id=user_id, is_admin=is_admin,
                )
        conn.close()
        return cur.rowcount > 0

    def _feed_user_rejection(self, dossier: LeadDossier, *, user_id: str = "",
                             is_admin: bool = False) -> None:
        """Teach fit-learning that this dossier's company+domain are NOT clients.

        Called only on rejection-class deletes. Company name and mail domain
        are recorded from the dossier itself (the SAME names the next discovery
        pass would surface), so a user's "not our client" delete is a
        permanent identity-level skip — gated by the corroboration rule:

        ``corroborated`` is True when an ADMIN rejected (the operator is
        trusted) or when the dossier's OWN research already said "not our
        client" (the AI's grounded verdict agrees with the user — two
        independent signals). An uncorroborated single-user rejection is
        still recorded (it counts toward the two-DISTINCT-users purge rule
        and the audit trail) but does not purge the identity alone.
        """
        from app.lead_research.fit_learning import FitLearningStore

        learning = FitLearningStore(self._db_path)
        corroborated = bool(
            is_admin or "not our client" in (dossier.fit or "").lower()
        )
        company = dossier.refined_company or dossier.company.name
        if company:
            learning.reject_company(company, user_id=user_id,
                                    corroborated=corroborated)
        domain = dossier.refined_domain or dossier.domain
        if domain:
            learning.reject_domain(domain, user_id=user_id,
                                   corroborated=corroborated)

    def deleted_log(self, limit: int = 100) -> dict[str, object]:
        """The admin audit feed: who deleted which email, when, why, and the
        admin's answer so far (pending / confirmed / restored). Newest first."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT email, deleted_at, reason, user_id, username, admin_decision "
            "FROM deleted_leads ORDER BY deleted_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM deleted_leads").fetchone()[0]
        conn.close()
        return {
            "total": total,
            "deleted": [
                {
                    "email": r[0], "deleted_at": r[1], "reason": r[2],
                    "user_id": r[3], "username": r[4] or r[3] or "unknown",
                    "admin_decision": r[5],
                }
                for r in rows
            ],
        }

    def deleted_emails(self) -> set[str]:
        """The delete-suppression pool: every email deleted and NOT restored.

        Requirement: "jo b data ay wo cache ma store hota rahe taay feature ma
        wo data dobara na dikhy" — a deleted lead (any reason) must never
        resurface to ANY user. Intake gates drop these emails before they are
        served or researched; an admin Restore is the only way out.
        """
        conn = self._conn()
        rows = conn.execute(
            "SELECT email FROM deleted_leads WHERE admin_decision <> 'restored'"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}

    def taken_by_others(self, user_id: str) -> set[str]:
        """Lead exclusivity (2026-09-12): emails already owned by ANOTHER user.

        "ak lead ya email sirf ak user ko show honi chahye" — an email is
        blocked for ``user_id`` when it has at least one owner (the dossiers
        user_id column or a dossier_owners row) and ``user_id`` is NOT among
        them. Legacy un-owned rows (user_id='') never block anyone. Runs once
        per job and the set is checked in-memory at the intake gates.
        """
        if not user_id:
            return set()
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT d.email FROM dossiers d
            WHERE d.email <> ''
              AND (
                    (d.user_id <> '' AND d.user_id <> ?)
                    OR EXISTS (SELECT 1 FROM dossier_owners o
                               WHERE o.email_hash = d.email_hash AND o.user_id <> ?)
              )
              AND NOT EXISTS (SELECT 1 FROM dossier_owners m
                              WHERE m.email_hash = d.email_hash AND m.user_id = ?)
            """,
            (user_id, user_id, user_id),
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}

    def owned_by_another(self, email: str, user_id: str) -> bool:
        """Live per-email exclusivity check (the research-time race guard).

        ``taken_by_others`` is a snapshot computed at run start; two users'
        runs racing the same email can BOTH pass the intake gates before
        either saves. This check re-reads ownership at the cache-hit moment:
        True when the email has an owner and ``user_id`` is not among them.
        """
        if not user_id:
            return False
        eh = _email_hash(email)
        conn = self._conn()
        row = conn.execute(
            """
            SELECT d.user_id,
                   EXISTS (SELECT 1 FROM dossier_owners m
                           WHERE m.email_hash = d.email_hash AND m.user_id = ?),
                   EXISTS (SELECT 1 FROM dossier_owners o
                           WHERE o.email_hash = d.email_hash AND o.user_id <> ?)
            FROM dossiers d WHERE d.email_hash = ?
            """,
            (user_id, user_id, eh),
        ).fetchone()
        conn.close()
        if row is None:
            return False
        first_owner, mine, other_owner = row
        if mine:
            return False  # the caller is an owner — their lead
        if first_owner:
            return first_owner != user_id
        return bool(other_owner)

    def confirm_deleted(self, email: str) -> dict[str, str] | None:
        """The admin's CONFIRM answer on a user delete (the delete-feed loop).

        Marks the row ``confirmed`` and — when the delete carried a rejection
        verdict — re-feeds it as an ADMIN rejection, which is corroborated and
        therefore decisive (the operator backed the user's "not our client").
        Returns the updated row summary, or None when the email was never
        deleted.
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT reason, dossier_json FROM deleted_leads WHERE email = ?",
            (email,),
        ).fetchone()
        if row is None:
            conn.close()
            return None
        conn.execute(
            "UPDATE deleted_leads SET admin_decision = 'confirmed' WHERE email = ?",
            (email,),
        )
        conn.commit()
        conn.close()

        reason, payload = row
        if _is_rejection_reason(reason) and payload:
            self._feed_user_rejection(
                LeadDossier.from_dict(json.loads(payload)),
                user_id="admin", is_admin=True,
            )
        return {"email": email, "admin_decision": "confirmed"}

    def restore_deleted(self, email: str) -> dict[str, str] | None:
        """The admin's RESTORE answer: bring a wrongly-deleted lead back.

        Re-saves the stashed dossier (assigned back to the user who deleted
        it), marks the row ``restored`` (which lifts the suppression — the
        email may resurface again), and CLEARS the identity-learning rows the
        delete had fed: the admin's word that the verdict was wrong reopens
        the company/domain for everyone. Returns the restored summary, or
        None when there is nothing to restore (never deleted / no snapshot).
        """
        from app.lead_research.fit_learning import FitLearningStore

        conn = self._conn()
        row = conn.execute(
            "SELECT reason, dossier_json, user_id FROM deleted_leads "
            "WHERE email = ?",
            (email,),
        ).fetchone()
        if row is None:
            conn.close()
            return None
        reason, payload, owner_id = row
        conn.execute(
            "UPDATE deleted_leads SET admin_decision = 'restored' WHERE email = ?",
            (email,),
        )
        conn.commit()
        conn.close()
        if not payload:
            # Pre-snapshot delete (no dossier stash): restore only lifts the
            # suppression — the lead can be re-discovered fresh.
            return {"email": email, "admin_decision": "restored", "restored_dossier": "no"}

        dossier = LeadDossier.from_dict(json.loads(payload))
        self.save(dossier, user_id=owner_id)
        # The delete fed a rejection verdict the admin now says was wrong —
        # clear the identity rows so the company/domain reopens for everyone.
        if _is_rejection_reason(reason):
            learning = FitLearningStore(self._db_path)
            company = dossier.refined_company or dossier.company.name
            if company:
                learning.clear_rejection_company(company)
            domain = dossier.refined_domain or dossier.domain
            if domain:
                learning.clear_rejection_domain(domain)
        return {"email": email, "admin_decision": "restored", "restored_dossier": "yes"}

    # ------------------------------------------------------------------
    # User organization metadata (folders + tags) — Phase B.
    # Files live in their OWN columns; the research payload is untouched.
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(folder: str, tags: list[str]) -> tuple[str, str]:
        """Normalize user input: trim folder, dedupe+trim tags (empty dropped,
        original order kept). Returns (folder, tags_json)."""
        f = (folder or "").strip()
        seen: set[str] = set()
        kept: list[str] = []
        for t in tags or []:
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                kept.append(t)
        return f, json.dumps(kept)

    def set_meta(self, email: str, *, folder: str = "", tags: list[str] | None = None) -> bool:
        """Set a dossier's folder + tags; False when no such dossier exists.

        Pure metadata update — `dossier_json` is never written here, so the
        researched payload is preserved exactly as the pipeline saved it.
        """
        f, tags_json = self._normalize(folder, tags)
        eh = _email_hash(email)
        conn = self._conn()
        cur = conn.execute(
            "UPDATE dossiers SET folder = ?, tags = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE email_hash = ?",
            (f, tags_json, eh),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def get_meta(self, email: str) -> LeadMeta | None:
        """A dossier's folder + tags, or None when the dossier is absent."""
        eh = _email_hash(email)
        conn = self._conn()
        row = conn.execute(
            "SELECT folder, tags FROM dossiers WHERE email_hash = ?", (eh,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return LeadMeta.from_db(row[0], row[1])

    def all_meta(self, user_id: str = "", is_admin: bool = False,
                 include_legacy: bool = False) -> dict[str, LeadMeta]:
        """Map email_hash -> folder+tags for every stored dossier (one query).

        Per-user isolation: a non-admin only sees metadata for their OWN
        dossiers (admin, or no filter, sees everything); ``include_legacy``
        adds the pre-auth (``user_id=''``) rows.
        """
        conn = self._conn()
        args: list[Any] = []
        conds = ""
        if user_id and not is_admin:
            shared = self._shared_visible()
            if include_legacy:
                conds = f"WHERE (d.user_id = ? OR d.user_id = '' OR {shared})"
            else:
                conds = f"WHERE (d.user_id = ? OR {shared})"
            args = [user_id, user_id]
        rows = conn.execute(
            f"SELECT d.email_hash, d.folder, d.tags FROM dossiers d {conds}",
            args,
        ).fetchall()
        conn.close()
        return {r[0]: LeadMeta.from_db(r[1], r[2]) for r in rows}

    # ------------------------------------------------------------------
    # CRM pipeline (Phase E1): stage + next action on the dossier row, and
    # an append-only timeline in crm_events. Same survival rule as folder/
    # tags — dossier_json is never written here.
    # ------------------------------------------------------------------

    def set_crm(self, email: str, *, status: str | None = None,
                next_action: str | None = None, note: str = "",
                user_id: str = "", username: str = "") -> dict[str, Any] | None:
        """Update one lead's CRM state — pipeline stage, next action, and/or
        an appended note (all optional; only what is provided is touched).

        Returns ``{"crm_status", "next_action"}`` after the update, or None
        when no such dossier exists. Every ACTUAL change appends one
        ``crm_events`` row (kind = status / next_action / note), so the
        timeline only ever records real transitions — a no-op call (same
        stage, same note text) writes nothing, never a fake event.
        """
        eh = _email_hash(email)
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT crm_status, next_action FROM dossiers WHERE email_hash = ?",
                (eh,),
            ).fetchone()
            if row is None:
                return None
            old_status, old_action = row[0] or "", row[1] or ""

            def _event(kind: str, detail: str) -> None:
                conn.execute(
                    "INSERT INTO crm_events "
                    "(email_hash, email, user_id, username, kind, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (eh, email, user_id, username, kind, detail),
                )

            if status is not None and status != old_status:
                conn.execute(
                    "UPDATE dossiers SET crm_status = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE email_hash = ?",
                    (status, eh),
                )
                _event("status", f"{old_status or 'researched'} → {status}")
            if next_action is not None and next_action.strip() != old_action:
                na = next_action.strip()
                conn.execute(
                    "UPDATE dossiers SET next_action = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE email_hash = ?",
                    (na, eh),
                )
                _event("next_action", na if na else "(cleared)")
            if note.strip():
                _event("note", note.strip())
            conn.commit()
            final = conn.execute(
                "SELECT crm_status, next_action FROM dossiers WHERE email_hash = ?",
                (eh,),
            ).fetchone()
            return {"crm_status": final[0] or "", "next_action": final[1] or ""}
        finally:
            conn.close()

    def get_crm(self, email: str) -> dict[str, Any] | None:
        """One lead's CRM state: stage + next action + the timeline (newest
        last, chronological). None when no such dossier exists."""
        eh = _email_hash(email)
        conn = self._conn()
        row = conn.execute(
            "SELECT crm_status, next_action FROM dossiers WHERE email_hash = ?",
            (eh,),
        ).fetchone()
        if row is None:
            conn.close()
            return None
        events = conn.execute(
            "SELECT id, kind, detail, user_id, username, created_at "
            "FROM crm_events WHERE email_hash = ? ORDER BY id ASC LIMIT 200",
            (eh,),
        ).fetchall()
        conn.close()
        return {
            "crm_status": row[0] or "",
            "next_action": row[1] or "",
            "events": [
                {"id": r[0], "kind": r[1], "detail": r[2],
                 "user_id": r[3] or "", "username": r[4] or "",
                 "created_at": r[5] or ""}
                for r in events
            ],
        }

    # -- Admin visibility flag (Dashboard data control) ---------------------

    def set_hidden(self, email: str, hidden: bool) -> bool:
        """Mark ONE dossier hidden (True = hidden from user views) / visible.

        Pure metadata — ``dossier_json`` is untouched (same rule as ``set_meta``).
        The row stays in the DB and in the admin's unfiltered dashboard; only the
        USER-facing lead views filter it. False when no such dossier exists.
        """
        eh = _email_hash(email)
        conn = self._conn()
        cur = conn.execute(
            "UPDATE dossiers SET hidden = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE email_hash = ?",
            (1 if hidden else 0, eh),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def set_hidden_bulk(self, emails: list[str], hidden: bool) -> int:
        """Mark many dossiers hidden/visible in ONE statement; returns how many.

        Used by the date/search admin scopes. Empty input is an honest 0. Invalid
        emails (no matching hash) simply don't count — never an error (CLAUDE.md §6).
        """
        emails = list(dict.fromkeys(e or "" for e in emails))
        emails = [e for e in emails if e]
        if not emails:
            return 0
        conn = self._conn()
        placeholders = ",".join("?" for _ in emails)
        target = 1 if hidden else 0
        params: list[Any] = [target, target]
        params += [_email_hash(e) for e in emails]
        cur = conn.execute(
            f"UPDATE dossiers SET hidden = ?, updated_at = CURRENT_TIMESTAMP "
            f"WHERE hidden <> ? AND email_hash IN ({placeholders})",
            params,
        )
        conn.commit()
        conn.close()
        return cur.rowcount

    def hidden_hashes(self) -> set[str]:
        """Email hashes whose dossier is `hidden` from the user views."""
        conn = self._conn()
        rows = conn.execute("SELECT email_hash FROM dossiers WHERE hidden = 1").fetchall()
        conn.close()
        return {r[0] for r in rows}

    def set_user_bulk(self, emails: list[str], user_id: str) -> int:
        """Assign/unassign many dossiers to one user in ONE statement.

        The admin's "push this folder to user X" control: setting ``user_id``
        makes the leads appear on that user's dashboard (their own exact-match
        view); ``user_id=''`` takes them back to admin-only. The admin panel
        keeps seeing every dossier regardless. Returns how many rows moved —
        an honest count, never silent (CLAUDE.md §6).
        """
        emails = [e for e in dict.fromkeys(e or "" for e in emails) if e]
        if not emails:
            return 0
        conn = self._conn()
        placeholders = ",".join("?" for _ in emails)
        params: list[Any] = [user_id, user_id]
        params += [_email_hash(e) for e in emails]
        cur = conn.execute(
            f"UPDATE dossiers SET user_id = ? "
            f"WHERE user_id <> ? AND email_hash IN ({placeholders})",
            params,
        )
        # Keep the sharing junction in step with the admin's control: assign
        # makes the target user a durable owner (visible even if the column
        # later moves again); unassign ('') is "back to admin-only" — every
        # owner row goes too, or the leads would stay on user dashboards.
        if user_id:
            conn.execute(
                f"INSERT OR IGNORE INTO dossier_owners (email_hash, user_id) "
                f"SELECT email_hash, ? FROM dossiers "
                f"WHERE email_hash IN ({placeholders})",
                [user_id] + params[2:],
            )
        else:
            conn.execute(
                f"DELETE FROM dossier_owners WHERE email_hash IN ({placeholders})",
                params[2:],
            )
        conn.commit()
        conn.close()
        return cur.rowcount

    def visibility_by_date(self) -> dict[str, dict[str, int]]:
        """``{YYYY-MM-DD: {total, hidden}}`` — the admin's per-date data control view.

        Reuses :meth:`research_dates` + :meth:`hidden_hashes` (no new query shape):
        which dates hold data, and how much of each date is currently hidden from
        the user dashboard. The admin picks a row here to Hide / Show / Delete.
        """
        hidden = self.hidden_hashes()
        out: dict[str, dict[str, int]] = {}
        for eh, date in self.research_dates().items():
            if not date:
                continue
            rec = out.setdefault(date, {"total": 0, "hidden": 0})
            rec["total"] += 1
            if eh in hidden:
                rec["hidden"] += 1
        return out

    def research_dates(self) -> dict[str, str]:
        """Map email_hash -> the research date (``YYYY-MM-DD``) of each dossier.

        The date a lead was first researched/persisted (``created_at``) is the
        honest "kis tareekh ko nikala" the list/detail views surface — never a
        rebuilt timestamp, the actual row the pipeline wrote.
        """
        conn = self._conn()
        rows = conn.execute(
            "SELECT email_hash, date(created_at) FROM dossiers"
        ).fetchall()
        conn.close()
        return {r[0]: (r[1] or "") for r in rows}

    # -- Persisted filter columns + SQL paging (Phase 2, frontend speed) -----

    def set_filter_columns(self, email_hash: str, recommendation: str,
                           potential_score: float, bound: bool) -> None:
        """Heal ONE row's persisted verdict to today's rules (self-healing drift).

        list_leads re-gates its page and writes any drift back here, so a rule
        change corrects the column the moment the row is next viewed — the next
        request then filters it correctly up-front.
        """
        conn = self._conn()
        conn.execute(
            "UPDATE dossiers SET recommendation = ?, potential_score = ?, bound = ? "
            "WHERE email_hash = ?",
            (recommendation, float(potential_score), 1 if bound else 0, email_hash),
        )
        conn.commit()
        conn.close()

    def _filter_where(self, *, recommendation: str | None = None,
                      bound: bool | None = None, min_score: float | None = None,
                      folder: str | None = None, tag: str | None = None,
                      date: str | None = None, source_emails: set[str] | None = None,
                      q: str | None = None, global_scope: bool = False,
                      user_id: str | None = None, is_admin: bool = False,
                      include_legacy: bool = False,
                      crm_status: str | None = None,
                      ) -> tuple[str, list[Any]]:
        """Shared WHERE clause for the user-facing lead views.

        Mirrors the old Python filtering in :func:`app.api.v1.leads.list_leads`
        exactly —``hidden`` always excluded, ``recommendation=None`` means
        actionable-only (``skip`` and ungated ``''`` hidden), ``folder=None`` is
        the UNFILED inbox unless a date/tag makes the view GLOBAL (no place
        scoping), ``folder='*'`` is every place. ``q`` searches the identity
        fields via ``json_extract`` (substring, case-insensitive LIKE, literal-safe
        escapes so a query with %/_/\\ is a literal search, never a wildcard).

        Per-user isolation: non-admin users see dossiers they own — the
        ``user_id`` column (first researcher) OR a :data:`dossier_owners` row
        (their own search surfaced it — cross-user sharing, Phase 2). With
        ``include_legacy`` the caller ALSO sees the legacy pre-auth rows
        (``user_id=''``) — the admin's OWN dashboard uses this: own + legacy
        data, never other users' searches (those live in the admin panel
        instead). The unfiltered whole-store view is the admin panel's job,
        not a user view.
        """
        conds = ["d.hidden = 0"]
        # Per-user data isolation (+ cross-user sharing)
        if user_id and not is_admin:
            shared = self._shared_visible()
            if include_legacy:
                # Own + legacy pre-auth rows — the admin's own-dashboard scope.
                conds.append(f"(d.user_id = ? OR d.user_id = '' OR {shared})")
            else:
                # Own only — legacy (user_id='') dossiers belong to the admin
                # alone; a new user's dashboard stays fresh. A dossier_owners
                # row means the user's OWN search surfaced this lead (shared
                # research, not mixing — it is exactly what they searched).
                conds.append(f"(d.user_id = ? OR {shared})")
            args: list[Any] = [user_id, user_id]
        else:
            args = []
        if recommendation is None:
            conds.append("d.recommendation NOT IN ('skip', '')")
        elif recommendation == "*":
            pass  # every recommendation incl skip/ungated (the CSV "all" path)
        else:
            conds.append("d.recommendation = ?")
            args.append(recommendation)
        if bound is not None:
            conds.append("d.bound = ?")
            args.append(1 if bound else 0)
        if crm_status:
            # CRM pipeline stage (Phase E1) — '' (pre-migration rows) reads as
            # 'researched', so filtering by researched matches them too.
            if crm_status == "researched":
                conds.append("d.crm_status IN ('researched', '')")
            else:
                conds.append("d.crm_status = ?")
                args.append(crm_status)
        if min_score is not None:
            conds.append("d.potential_score >= ?")
            args.append(float(min_score))
        if folder is None:
            if not global_scope:
                conds.append("d.folder = ''")  # default = the Unfiled inbox
        elif folder != "*":
            conds.append("d.folder = ?")
            args.append(folder)
        if tag:
            conds.append("EXISTS (SELECT 1 FROM json_each(d.tags) AS jt WHERE jt.value = ?)")
            args.append(tag)
        if date:
            conds.append("date(d.created_at) = ?")
            args.append(date)
        if source_emails:
            ems = [e for e in dict.fromkeys(source_emails) if e]
            if ems:
                conds.append(f"d.email IN ({','.join('?' for _ in ems)})")
                args.extend(ems)
        if q and q.strip():
            lit = (q or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{lit}%"
            conds.append(
                "(d.email LIKE ? ESCAPE '\\' OR d.domain LIKE ? ESCAPE '\\' "
                "OR json_extract(d.dossier_json, '$.company.name') LIKE ? ESCAPE '\\' "
                "OR json_extract(d.dossier_json, '$.refined_company') LIKE ? ESCAPE '\\' "
                "OR json_extract(d.dossier_json, '$.person.name') LIKE ? ESCAPE '\\' "
                "OR json_extract(d.dossier_json, '$.person.role') LIKE ? ESCAPE '\\')"
            )
            args.extend([like] * 6)
        return " AND ".join(conds), args

    #: The columns :meth:`query_leads` / :meth:`all_matching` read.
    _LIST_COLS = (
        "d.email_hash, d.email, d.domain, "
        "json_extract(d.dossier_json, '$.company.name') AS _cn, d.dossier_json, "
        "d.folder, d.tags, date(d.created_at), "
        "d.recommendation, d.potential_score, d.bound, "
        "d.crm_status, d.next_action"
    )

    @staticmethod
    def _row_to_lead(r) -> dict[str, Any]:
        """Row -> the record both SQL readers return."""
        return {
            "email_hash": r[0], "email": r[1], "domain": r[2],
            "dossier": LeadDossier.from_dict(json.loads(r[4])),
            "folder": r[5] or "", "tags": LeadMeta.from_db(r[5], r[6]).tags,
            "created_at": r[7] or "",
            "recommendation": r[8], "potential_score": r[9], "bound": r[10],
            # CRM pipeline state (Phase E1); '' = the pre-migration default,
            # read honestly as 'researched'.
            "crm_status": r[11] or "researched", "next_action": r[12] or "",
        }

    def query_leads(self, *, limit: int = 100, offset: int = 0, **filters) -> tuple[list[dict[str, Any]], int]:
        """SQL filter + sort + paginate — the whole-store materialize is gone.

        Returns ``(page, total)``: only the PAGE is parsed (a page, not the whole
        store); ``total`` is the honest count for the SAME filters (drives the
        X-Total-Count header so the UI can page). Each record carries ``dossier``
        (LeadDossier), ``folder``, ``tags``, ``created_at`` and the persisted
        verdict columns, so the caller's freshness re-gate has what it needs.

        Ordering is DISCOVERY ORDER (``rowid ASC`` — the order leads were found,
        number-wise), matching :meth:`all_matching`. The old tier -> score sort
        re-ranked rows on every refresh and leads "jumped to the bottom" the
        moment a verdict/score was re-gated; discovery order is stable.
        """
        where, args = self._filter_where(**filters)  # user_id/is_admin pass via filters
        conn = self._conn()
        total = conn.execute(
            f"SELECT COUNT(*) FROM dossiers d WHERE {where}", args
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT {self._LIST_COLS} FROM dossiers d WHERE {where} "
            "ORDER BY d.rowid ASC "
            "LIMIT ? OFFSET ?",
            args + [int(limit), int(offset)],
        ).fetchall()
        conn.close()
        return [self._row_to_lead(r) for r in rows], total

    def all_matching(self, **filters) -> list[dict[str, Any]]:
        """Every dossier matching the user-view filters (no pagination) — the
        CSV-export path. Same WHERE as :meth:`query_leads`, so export honours the
        exact same filters the list shows (hidden + actionable-only included).
        """
        where, args = self._filter_where(**filters)
        conn = self._conn()
        rows = conn.execute(
            f"SELECT {self._LIST_COLS} FROM dossiers d WHERE {where} ORDER BY d.rowid ASC",
            args,
        ).fetchall()
        conn.close()
        return [self._row_to_lead(r) for r in rows]

    def distinct_dates(self, user_id: str = "", is_admin: bool = False,
                       include_legacy: bool = False) -> list[str]:
        """Every extraction date present on a VISIBLE dossier, newest first — one
        DISTINCT GROUP, no per-row Python (the old list_dates full scan). A date
        whose only rows are admin-hidden drops out of the dropdown (the user can no
        longer recall it).

        Per-user isolation: a non-admin only sees dates from their OWN dossiers;
        admin (or no filter) sees every date; ``include_legacy`` adds the pre-auth
        rows to the caller's own view.
        """
        conn = self._conn()
        args: list[Any] = []
        conds = "WHERE hidden = 0 AND date(created_at) IS NOT NULL"
        if user_id and not is_admin:
            shared = self._shared_visible()
            conds += f" AND (d.user_id = ? OR d.user_id = '' OR {shared})" if include_legacy else f" AND (d.user_id = ? OR {shared})"
            args = [user_id, user_id]
        rows = conn.execute(
            "SELECT DISTINCT date(created_at) AS d FROM dossiers d "
            f"{conds} ORDER BY d DESC",
            args,
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]

    def tag_counts(self, user_id: str = "", is_admin: bool = False,
                   include_legacy: bool = False) -> list[dict[str, Any]]:
        """Global tag counts for the USER views (hidden + skip/ungated excluded —
        the same set the actionable list shows). Feeds the tag chips/filter, one
        GROUP BY over ``json_each`` instead of a full-list download.

        Per-user isolation: non-admin only counts their OWN dossiers' tags.
        """
        conn = self._conn()
        args: list[Any] = []
        conds = "d.hidden = 0 AND d.recommendation NOT IN ('skip', '')"
        if user_id and not is_admin:
            shared = self._shared_visible()
            conds += f" AND (d.user_id = ? OR d.user_id = '' OR {shared})" if include_legacy else f" AND (d.user_id = ? OR {shared})"
            args = [user_id, user_id]
        rows = conn.execute(
            "SELECT jt.value AS tag, COUNT(*) AS cnt "
            "FROM dossiers d, json_each(d.tags) AS jt "
            f"WHERE {conds} "
            "GROUP BY jt.value ORDER BY cnt DESC, tag COLLATE NOCASE ASC",
            args,
        ).fetchall()
        conn.close()
        return [{"tag": r[0], "count": r[1]} for r in rows]

    # -- Folders catalog (Phase B.2) ------------------------------------

    @staticmethod
    def _backfill_folders(conn: sqlite3.Connection) -> None:
        """Self-heal the catalog with folder names used on dossiers.

        Keeps the store consistent for legacy leads (folder values that pre-date
        the catalog) AND for a rename/clear that moves leads — every real folder
        name ends up present as a clickable group, empty or not.

        Multi-user: the row is attributed to the DOSSIER'S owner, so a folder
        name files itself into the account that actually uses it — never into
        everyone's catalog.
        """
        conn.execute(
            "INSERT OR IGNORE INTO folders(user_id, name) "
            "SELECT DISTINCT user_id, folder FROM dossiers WHERE folder <> ''"
        )

    def create_folder(self, name: str, user_id: str = "") -> bool:
        """Create a persisted (possibly empty) folder; True when newly created.

        The Phase B.2 contract: a folder exists FIRST (``"Monday data"``), is
        clickable in the filter even with zero leads, and fills as the user
        moves leads into it. Duplicate names are ignored (idempotent).

        Multi-user: the row is OWNED by ``user_id`` ("" = legacy/admin own
        view) — two accounts may each have a folder of the same name without
        ever seeing each other's group.
        """
        name = (name or "").strip()
        if not name:
            return False
        conn = self._conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO folders(user_id, name) VALUES (?, ?)",
            (user_id, name),
        )
        conn.commit()
        conn.close()
        return cur.rowcount == 1

    def list_folders(self, user_id: str = "", is_admin: bool = False,
                     include_legacy: bool = False) -> list[dict[str, Any]]:
        """Every OWNED catalog folder with its live lead count, newest first.

        Returns ``[{name, created_at, count}]`` — empty folders included (that
        is the point of the catalog). ``_backfill_folders`` makes sure a folder
        name that only rides on leads (legacy or post-rename) is never missing.

        Multi-user: rows are owned. A non-admin lists only THEIR folder rows
        (+ the legacy '' rows on the admin's own dashboard); ``is_admin=True``
        (admin panel) lists every account's rows.
        """
        conn = self._conn()
        self._backfill_folders(conn)
        if is_admin:
            owner_sql, owner_args = "", []
        elif include_legacy:
            owner_sql, owner_args = "WHERE f.user_id IN (?, '')", [user_id]
        else:
            owner_sql, owner_args = "WHERE f.user_id = ?", [user_id]
        args: list[Any] = []
        conds = "d.hidden = 0 AND d.recommendation NOT IN ('skip', '')"
        if user_id and not is_admin:
            shared = self._shared_visible()
            conds += f" AND (d.user_id = ? OR d.user_id = '' OR {shared})" if include_legacy else f" AND (d.user_id = ? OR {shared})"
            args = [user_id, user_id]
        rows = conn.execute(
            f"SELECT f.name, f.created_at, "
            f"(SELECT COUNT(*) FROM dossiers d WHERE d.folder = f.name AND {conds}) AS cnt "
            f"FROM folders f {owner_sql} "
            f"ORDER BY f.created_at DESC, f.name COLLATE NOCASE ASC",
            owner_args + args,
        ).fetchall()
        conn.commit()  # backfill may have inserted rows
        conn.close()
        return [
            {"name": r[0], "created_at": (r[1] or "")[:19], "count": r[2]}
            for r in rows
        ]

    def folder_catalog(self, user_id: str = "", is_admin: bool = False,
                       include_legacy: bool = False) -> dict[str, Any]:
        """The organization mailbox overview — one round-trip for the UI.

        ``{folders: [{name, created_at, count}], unfiled, total}``: ``unfiled``
        is what the DEFAULT Companies view shows (leads still in the inbox,
        ``folder = ''``); ``total`` counts every dossier (folders included), so
        the "All" chip and Dashboard totals stay honest. Same self-healing
        backfill as :meth:`list_folders`.

        COUNTS = THE LIST, NOT THE STORE — the root cause of the "chip says
        313 but the view shows 38" bug. The leads list hides ``skip``/junk
        dossiers by default (a dead-domain / non-construction / sub-threshold
        dossier is not a lead), so the catalog applies the SAME persisted
        ``recommendation`` column (the gate written at save-time, identical to
        read-time re-gate) and counts only what the list would show — all in SQL,
        never materializing the store. Junk still exists in the store (purgeable
        via "Clear junk") — it is just never counted as a lead.

        Per-user isolation: non-admin counts only their OWN dossiers; admin sees
        all (legacy empty-user_id rows included); ``include_legacy`` gives a
        caller own + legacy instead.
        """
        conn = self._conn()
        self._backfill_folders(conn)
        if is_admin:
            rows = conn.execute(
                "SELECT name, created_at FROM folders "
                "ORDER BY created_at DESC, name COLLATE NOCASE ASC"
            ).fetchall()
        elif include_legacy:
            rows = conn.execute(
                "SELECT name, created_at FROM folders WHERE user_id IN (?, '') "
                "ORDER BY created_at DESC, name COLLATE NOCASE ASC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, created_at FROM folders WHERE user_id = ? "
                "ORDER BY created_at DESC, name COLLATE NOCASE ASC",
                (user_id,),
            ).fetchall()
        args: list[Any] = []
        conds = "WHERE hidden = 0 AND recommendation NOT IN ('skip', '')"
        if user_id and not is_admin:
            shared = self._shared_visible()
            conds += f" AND (d.user_id = ? OR d.user_id = '' OR {shared})" if include_legacy else f" AND (d.user_id = ? OR {shared})"
            args = [user_id, user_id]
        count_rows = conn.execute(
            f"SELECT folder, COUNT(*) FROM dossiers d {conds} GROUP BY folder",
            args,
        ).fetchall()
        conn.commit()  # backfill may have inserted rows
        conn.close()

        by_folder = {r[0]: r[1] for r in count_rows}
        total = sum(by_folder.values())
        unfiled = by_folder.get("", 0)
        listed = {name for name, _ in rows}
        # Honesty union: a folder name on a VISIBLE dossier (own OR shared)
        # must appear with its count even when this user owns no catalog row
        # for it — a shared lead filed by another account still needs its
        # place in THIS view, and the chips must always sum to ``total``
        # (the "chip says 313 but the view shows 38" class of bug).
        extra = [
            {"name": name, "created_at": "", "count": count}
            for name, count in sorted(by_folder.items(), key=lambda kv: kv[0].lower())
            if name and name not in listed
        ]
        return {
            "folders": [
                {"name": name, "created_at": (created or "")[:19],
                 "count": by_folder.get(name, 0)}
                for name, created in rows
            ] + extra,
            "unfiled": unfiled,
            "total": total,
        }

    def rename_folder(self, old: str, new: str, user_id: str = "",
                      is_admin: bool = False, include_legacy: bool = False) -> int:
        """Rename a folder across every dossier AND the catalog; count renamed.

        The sweep is unchanged (folder rides on dossiers); the catalog keeps the
        group first-class. An empty folder that is renamed stays an empty group
        under the new name — nothing is lost.

        Per-user isolation: a non-admin renames only within their OWN dossiers
        (never touching another user's leads or legacy rows); the catalog row is
        kept for everyone who still holds the old name.
        """
        old = (old or "").strip()
        new = (new or "").strip()
        if not old or old == new:
            return 0
        affected = [
            eh for eh, m in self.all_meta(
                user_id=user_id, is_admin=is_admin, include_legacy=include_legacy
            ).items()
            if m.folder == old
        ]
        for eh in affected:
            self.set_meta_direct(eh, new, None)
        # Catalog swap, scoped to the CALLER'S owner rows: the sweep above moved
        # every visible dossier out of the old name, so this account's old-name
        # group is empty and goes — while another account's same-named folder is
        # theirs and stays untouched.
        conn = self._conn()
        if is_admin:
            # Admin-panel sweep is global: old rows go everywhere, and the
            # per-owner backfill re-attributes the new name on the next read.
            conn.execute("DELETE FROM folders WHERE name = ?", (old,))
            conn.execute(
                "INSERT OR IGNORE INTO folders(user_id, name) VALUES ('', ?)", (new,)
            )
        else:
            owners = [user_id] + ([""] if include_legacy else [])
            qmarks = ",".join("?" * len(owners))
            conn.execute(
                f"DELETE FROM folders WHERE name = ? AND user_id IN ({qmarks})",
                [old, *owners],
            )
            for owner in owners:
                conn.execute(
                    "INSERT OR IGNORE INTO folders(user_id, name) VALUES (?, ?)",
                    (owner, new),
                )
        conn.commit()
        conn.close()
        return len(affected)

    def rename_tag(self, old: str, new: str, user_id: str = "",
                   is_admin: bool = False, include_legacy: bool = False) -> int:
        """Rename a tag value across every dossier; return how many changed.

        Per-user isolation: a non-admin renames only within their OWN dossiers.
        """
        old = (old or "").strip()
        new = (new or "").strip()
        if not old or old == new:
            return 0
        affected = 0
        for eh, m in self.all_meta(
            user_id=user_id, is_admin=is_admin, include_legacy=include_legacy
        ).items():
            if old in m.tags:
                tags = [(new if t == old else t) for t in m.tags]
                self.set_meta_direct(eh, m.folder, tags)
                affected += 1
        return affected

    def clear_folder(self, value: str, user_id: str = "", is_admin: bool = False,
                     include_legacy: bool = False) -> int:
        """Remove a folder value from every dossier AND drop it from the catalog.

        Deleting a folder releases its leads (folder="") and removes the group —
        the honest count of released leads is returned (0 for an empty folder is
        a valid "folder deleted, nothing to release").

        Per-user isolation: a non-admin clears only within their OWN dossiers;
        the catalog group is dropped only when no dossier outside the radius
        still uses the name.
        """
        value = (value or "").strip()
        if not value:
            return 0
        affected = [
            eh for eh, m in self.all_meta(
                user_id=user_id, is_admin=is_admin, include_legacy=include_legacy
            ).items()
            if m.folder == value
        ]
        for eh in affected:
            self.set_meta_direct(eh, "", None)
        # Catalog drop, scoped to the CALLER'S owner rows (same rule as
        # rename_folder): this account's group goes; another account's
        # same-named folder is theirs and stays.
        conn = self._conn()
        if is_admin:
            conn.execute("DELETE FROM folders WHERE name = ?", (value,))
        else:
            owners = [user_id] + ([""] if include_legacy else [])
            qmarks = ",".join("?" * len(owners))
            conn.execute(
                f"DELETE FROM folders WHERE name = ? AND user_id IN ({qmarks})",
                [value, *owners],
            )
        conn.commit()
        conn.close()
        return len(affected)

    def clear_tag(self, value: str, user_id: str = "", is_admin: bool = False,
                  include_legacy: bool = False) -> int:
        """Remove a tag value from every dossier; return how many changed.

        Per-user isolation: a non-admin clears only within their OWN dossiers.
        """
        value = (value or "").strip()
        if not value:
            return 0
        affected = 0
        for eh, m in self.all_meta(
            user_id=user_id, is_admin=is_admin, include_legacy=include_legacy
        ).items():
            if value in m.tags:
                tags = [t for t in m.tags if t != value]
                self.set_meta_direct(eh, m.folder, tags)
                affected += 1
        return affected

    def set_meta_direct(self, email_hash: str, folder: str, tags: list[str] | None) -> None:
        """Shared row-write for the sweep helpers (already-normalized callers).

        ``tags=None`` keeps this row's existing tags (rename/clear on folder);
        a tag-list normalizes + replaces (rename/clear on a tag).
        """
        if tags is None:
            conn = self._conn()
            row = conn.execute(
                "SELECT tags FROM dossiers WHERE email_hash = ?", (email_hash,)
            ).fetchone()
            conn.close()
            tags = list(LeadMeta.from_db("", row[0]).tags) if row else []
        _, tags_json = self._normalize(folder, tags)
        conn = self._conn()
        conn.execute(
            "UPDATE dossiers SET folder = ?, tags = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE email_hash = ?",
            (folder, tags_json, email_hash),
        )
        conn.commit()
        conn.close()

    def clean_dead_domain_dossiers(self) -> int:
        """Delete every stored dossier rejected at the dead-domain gate.

        These dossiers must never inflate lead totals (CLAUDE.md honest-logging
        rule) — a dead domain cannot receive email, so the address is not a
        lead. Returns how many were removed. Also removes the same emails from
        the discovery cache (``pending_leads``), so a dead-domain lead that was
        merely discovered is not served again on a future run.
        """
        removed = 0
        dead_emails: list[str] = []
        for dossier in self.list_all():
            if is_dead_domain_dossier(dossier):
                dead_emails.append(dossier.email)
        for email in dead_emails:
            if self.delete(email):
                removed += 1
        if dead_emails:
            # Mirror removal in the discovery cache when it shares this DB.
            pending = PendingLeadsStore(db_path=self._db_path)
            pending.remove(dead_emails)
        if removed:
            logger.info(
                "clean_dead_domain_dossiers: removed %d dead-domain dossier(s)",
                removed,
            )
        return removed


class PendingLeadsStore:
    """Discovery cache — discovered-but-not-yet-researched leads.

    When a discovery pass surfaces MORE emails than the user's target, the
    surplus is stored here so a later re-run can serve it WITHOUT paying for
    another live search. A lead leaves pending only when it is successfully
    researched (moved into the dossiers store).

    Lives in the same SQLite DB as :class:`LeadResearchStore` (one file).
    """

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            import os
            db_path = os.path.join(os.path.dirname(__file__), "..", "..", "output", "lead_research.db")
        self._db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        """Open a SQLite connection that WAITS under lock/IO contention.

        WAL is a persistent DB-header property (inherited per-connection), so a
        reader never blocks on a writer. But a NEW connection without a
        ``busy_timeout`` still fails instantly on residual transient contention
        — the uncaught consumer "disk I/O error" that silently stopped
        background research even though the pending buffer held hundreds of
        leads. Same risk-control the yield/candidate stores already apply
        (``PRAGMA busy_timeout = 5000``): a worker retries the lock briefly
        instead of throwing and dying mid-run (CLAUDE.md §6, background threads
        must be robust). Only the per-connection timeout needs setting each
        time; WAL rides along from the header.
        """
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        # Serialized (see _INIT_DB_LOCK) — same construction race as
        # LeadResearchStore; the two stores share the DB file.
        with _INIT_DB_LOCK:
            self._init_db_serialized()

    def _init_db_serialized(self) -> None:
        import os
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = self._conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_leads (
                email_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                domain TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                person TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                dead INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Additive migration — `dead` flags a confirmed dead-domain lead so it
        # is NEVER served again (the user's "same Skip emails every search"
        # complaint); `attempted_at`/`attempt_count` back the re-enrichment
        # cooldown (a research-ERROR lead is skipped for a while, not served
        # and re-failed on every Execute). Existing DBs predate the columns,
        # so add each guarded.
        _add_column(conn, "pending_leads", "dead", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "pending_leads", "attempted_at", "TIMESTAMP")
        _add_column(conn, "pending_leads", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        # ``dork`` (Phase G) — the Layer-1 dork template that surfaced this
        # lead, persisted so a cached lead researched in a LATER run can still
        # credit its producing dork's yield. Additive, default ''. Live DBs
        # predate the column; a plain ADD is safe ('' = unattributable).
        _add_column(conn, "pending_leads", "dork", "TEXT NOT NULL DEFAULT ''")
        # ``gated`` (Galti #3) — a cached row rejected by the serve-time vertical
        # gate. Rows cached BEFORE the non-client boundary existed (the DCTA
        # transit/mobility junk) were silently skipped by ``take``'s Python
        # filter but never left the head of the ``ORDER BY created_at ASC``
        # window, so every consumer claim returned empty forever: 20 minutes,
        # zero claims, zero dossiers, silent. Marking them lets the SQL window
        # ADVANCE past them — invisible to serving, exactly like ``dead``, same
        # additive guarded-ALTER pattern. Default 0 keeps old code compatible.
        _add_column(conn, "pending_leads", "gated", "INTEGER NOT NULL DEFAULT 0")
        # Trade routing (big-bang P1): the lead's OWN canonical trade slug
        # (tradefold). '' = unknown — served trade-agnostically (fail-open).
        # Stocked by ``add`` from the lead dict's ``trade`` key (threaded by
        # the pipeline from discovery records); P2's ``take(trade=...)`` will
        # serve from it. Pre-P1 rows are lazily backfilled below from the
        # company name — the only trade evidence a cached row carries.
        _add_column(conn, "pending_leads", "trade", "TEXT NOT NULL DEFAULT ''")
        self._backfill_pending_trade(conn)
        conn.commit()
        conn.close()

    @staticmethod
    def _backfill_pending_trade(conn: sqlite3.Connection) -> None:
        """Lazy trade backfill for cached rows saved before the column
        existed: fold the company NAME through the normalizer ("XYZ Drywall
        Inc" -> drywall). Weak evidence, so honestly '' when nothing
        clearly matches — never a guess. Idempotent (''-only scan; the
        unmappable tail is re-scanned each boot at negligible cost)."""
        from app.discovery.tradefold import normalize_trade

        rows = conn.execute(
            "SELECT email_hash, company FROM pending_leads WHERE trade = ''"
        ).fetchall()
        for eh, company in rows:
            trade = normalize_trade(company or "")
            if trade:
                conn.execute(
                    "UPDATE pending_leads SET trade = ? WHERE email_hash = ?",
                    (trade, eh),
                )

    def get(self, email: str) -> dict | None:
        eh = _email_hash(email)
        conn = self._conn()
        row = conn.execute(
            "SELECT email, domain, company, person, source_url, location, dork, trade "
            "FROM pending_leads WHERE email_hash = ?", (eh,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "email": row[0], "domain": row[1], "company": row[2],
            "person": row[3], "source_url": row[4], "location": row[5],
            "dork": row[6], "trade": row[7],
        }

    def add(self, leads: list[dict]) -> int:
        """Upsert a batch of lead dicts into pending; return count added.

        Free-mail addresses (gmail/aol/...) are NOT stocked — a consumer
        mailbox is no company lead ("koi lead nhi"). The research triage already
        demotes such an address to nurture, and the backlog sweep flags the
        historical rows; this guard stops NEW free-mail from ever occupying a
        cached surplus slot again (the permanent half of the total fix).
        """
        from app.email.email_cleaner import is_free_mail_domain
        from app.company_profile import get_profile

        conn = self._conn()
        added = 0
        for lead in leads:
            email = (lead.get("email") or "").strip()
            if "@" not in email:
                continue
            if is_free_mail_domain(email):
                continue
            # Vertical gate (root cause of the off-vertical cache flood): a lead
            # whose company/domain/source is a known non-client never occupies a
            # cached slot, so a later Execute can never serve or research it.
            # Same ONE boundary as the pipeline's fresh filter (CLAUDE.md §11).
            if get_profile().lead_is_non_client(
                company=lead.get("company", ""),
                domain=lead.get("domain", ""),
                source_url=lead.get("source_url", ""),
            ):
                logger.info(
                    "Pending drop (not our client): %s %s from %s",
                    email,
                    f"({lead.get('company')})" if lead.get("company") else "",
                    (lead.get("source_url") or "")[:90],
                )
                continue
            eh = _email_hash(email)
            # P1: discovery's folded trade label (tradefold.normalize_trade of
            # the record's trade_category — wired in leads.pipeline) stocks the
            # trade column; '' = honest unknown (P2's trade gate treats it as
            # not-this-trade, never serves it cross-trade).
            conn.execute("""
                INSERT INTO pending_leads
                    (email_hash, email, domain, company, person, source_url, location, dork, trade)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email_hash) DO UPDATE SET location = excluded.location
            """, (
                eh, email,
                lead.get("domain", ""),
                lead.get("company", ""),
                lead.get("person", ""),
                lead.get("source_url", ""),
                lead.get("location", ""),
                lead.get("dork", "") or lead.get("_discovery_dork", ""),
                lead.get("trade", ""),
            ))
            added += 1
        conn.commit()
        conn.close()
        return added

    def take(
        self, count: int, location: str = "", *, cooldown_seconds: int = 0,
    ) -> list[dict]:
        """Return up to ``count`` pending leads (oldest first), filtered by
        ``location`` when given. Leads stay in pending until ``remove`` — so a
        research failure keeps the lead available for a future retry.

        ``dead`` leads are EXCLUDED: a confirmed dead-domain address can never
        be served again as a recurring "Skip" (the user's complaint). The row
        is kept (so a deliberate future re-probe can clear the flag) but
        ``dead = 1`` rows are invisible to ``take``.

        ``cooldown_seconds`` (keyword-only, default 0 = disabled) ALSO excludes
        a lead re-attempted within the last ``cooldown_seconds``: a research-
        ERROR lead is recorded via :meth:`mark_attempt`, and while its cooldown
        is active it is skipped instead of being served and re-failed on every
        Execute (re-enrichment cooldown — no quality loss, the row is a REAL
        retry once the window expires). ``attempted_at`` NULL (never tried)
        rows are always served.
        """
        since = (
            f"-{int(cooldown_seconds)} seconds"
            if cooldown_seconds and cooldown_seconds > 0
            else None
        )
        conn = self._conn()
        if location:
            sql = (
                "SELECT email, domain, company, person, source_url, location, dork, trade "
                "FROM pending_leads WHERE location = ? AND dead = 0 AND gated = 0"
            )
            args: list[Any] = [location]
        else:
            sql = (
                "SELECT email, domain, company, person, source_url, location, dork, trade "
                "FROM pending_leads WHERE dead = 0 AND gated = 0"
            )
            args = []
        if since is not None:
            sql += (
                " AND (attempted_at IS NULL"
                " OR attempted_at <= datetime('now', ?))"
            )
            args.append(since)
        sql += " ORDER BY created_at ASC LIMIT ?"
        args.append(count)
        rows = conn.execute(sql, args).fetchall()
        # Same vertical gate as ``add``, applied at SERVE time too: rows cached
        # BEFORE the boundary existed (the DCTA transit/mobility junk) must never
        # be served again — they stay in the table (Phase C can purge to show
        # the user exactly what was excluded) but no research credit touches them.
        # Galti #3: a gate-rejected row must NOT keep occupying the head of the
        # created_at window — it can never be served, so it is marked ``gated``
        # here and the window advances to rows that CAN (a batch whose rows were
        # all rejected marks them all and serves the rows behind them on the
        # next call; without this, every consumer claim returns empty forever).
        from app.company_profile import get_profile

        gated_emails: list[str] = []
        served: list[dict] = []
        for r in rows:
            if get_profile().lead_is_non_client(
                company=r[2], domain=r[1], source_url=r[4]
            ):
                gated_emails.append(r[0])
                continue
            served.append(
                {"email": r[0], "domain": r[1], "company": r[2],
                 "person": r[3], "source_url": r[4], "location": r[5],
                 "dork": r[6], "trade": r[7]},
            )
        if gated_emails:
            # Persist the advance so the NEXT take() window starts AFTER the
            # marked rows instead of re-returning the same gated head forever.
            conn.executemany(
                "UPDATE pending_leads SET gated = 1 WHERE email = ?",
                [(e,) for e in gated_emails],
            )
            conn.commit()
            logger.info(
                "pending take: marked %d gate-rejected row(s) gated — queue "
                "advanced (served %d)", len(gated_emails), len(served),
            )
        conn.close()
        return served

    def cooling_emails(self, cooldown_seconds: int = 0) -> set[str]:
        """Emails currently inside the re-enrichment cooldown window.

        Used by the pipeline's FRESH-discovery filter too — not just the cache
        serve — so a cooled lead that a live search happens to surface again is
        not re-added and re-failed in the SAME run (``add`` keeps the attempt
        columns on conflict, but the fresh filter must also avoid it).
        """
        if not cooldown_seconds or cooldown_seconds <= 0:
            return set()
        conn = self._conn()
        rows = conn.execute(
            "SELECT email FROM pending_leads "
            "WHERE attempted_at IS NOT NULL "
            "AND attempted_at > datetime('now', ?)",
            (f"-{int(cooldown_seconds)} seconds",),
        ).fetchall()
        conn.close()
        return {row[0] for row in rows}

    def mark_attempt(self, emails: list[str]) -> int:
        """Record a failed research attempt so take() cools this lead down.

        A research ERROR leaves the lead in pending (it is NOT a dead domain,
        so ``dead`` stays 0) — without a timestamp the very next run would
        serve and re-fail it. Recording ``attempted_at = now`` + bumping
        ``attempt_count`` makes :meth:`take` skip it for the configured
        cooldown, then the row is a genuine retry afterwards. Returns how many
        rows were recorded (0 when an email was never cached).
        """
        if not emails:
            return 0
        conn = self._conn()
        recorded = 0
        for email in emails:
            cur = conn.execute(
                "UPDATE pending_leads SET attempted_at = CURRENT_TIMESTAMP, "
                "attempt_count = attempt_count + 1 WHERE email_hash = ?",
                (_email_hash(email),),
            )
            recorded += cur.rowcount
        conn.commit()
        conn.close()
        return recorded

    def mark_dead(self, emails: list[str]) -> int:
        """Flag confirmed dead-domain emails so they are never served again.

        The row is KEPT (not deleted) so a deliberate future re-probe can clear
        the flag, but ``take`` filters ``dead = 0`` — a dead lead can never
        re-surface run after run as the same "Skip". Returns how many rows were
        flagged (0 when the email was never cached — a live-only dead find).
        """
        if not emails:
            return 0
        conn = self._conn()
        flagged = 0
        for email in emails:
            cur = conn.execute(
                "UPDATE pending_leads SET dead = 1 WHERE email_hash = ?",
                (_email_hash(email),),
            )
            flagged += cur.rowcount
        conn.commit()
        conn.close()
        return flagged

    def dead_emails(self) -> set[str]:
        """Every email currently flagged dead (never serve, never re-research)."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT email FROM pending_leads WHERE dead = 1"
        ).fetchall()
        conn.close()
        return {row[0] for row in rows}

    def sweep_known_dead(
        self,
        *,
        dossier_store: Any | None = None,
        domain_delivers: Any | None = None,
    ) -> dict[str, int]:
        """One-pass pre-screen of the discovery cache — the TOTAL fix, not a
        4-email workaround.

        Every pending lead not yet flagged dead is checked with the SAME cheap
        gates the research stage uses, so a worthless address is ruled out
        BEFORE it ever consumes a research slot on the next run — no AI credits
        spent (the MX check is fast native DNS; free-mail triage is a set
        lookup):

          - already-researched (a dossier exists) -> row REMOVED (same stale
            purge the pipeline does at cache time, swept up-front);
          - free-mail domain (gmail/aol/...)       -> flagged ``dead`` (a
            consumer mailbox is not a company lead; never served);
          - no MX record on the EMAIL domain       -> flagged ``dead``
            (undeliverable — the exact gate that would SKIP it at research);
          - otherwise                              -> KEPT (a real, useful lead).

        Rows are FLAGGED, not deleted, so the ``dead`` pool also blocks a dead
        address from re-entering through FRESH discovery. Returns honest
        per-bucket counts (CLAUDE.md §6) so the user sees exactly what was kept
        vs removed — nothing silent.
        """
        from app.email.email_cleaner import is_free_mail_domain
        from app.lead_research.company_research import domain_delivers_email

        if domain_delivers is None:
            domain_delivers = domain_delivers_email

        conn = self._conn()
        rows = conn.execute(
            "SELECT email FROM pending_leads WHERE dead = 0"
        ).fetchall()
        conn.close()

        stats = {
            "total": len(rows),
            "kept": 0,
            "free_mail": 0,
            "dead_domain": 0,
            "already_researched": 0,
        }
        to_flag: list[str] = []
        to_remove: list[str] = []
        for (email,) in rows:
            email_domain = (
                (email or "").rsplit("@", 1)[-1].strip().lower()
                if "@" in (email or "") else ""
            )
            if dossier_store is not None and dossier_store.get(email) is not None:
                # Already a saved dossier — a served-and-researched address has
                # no business sitting in the cache (same as the stale purge).
                to_remove.append(email)
                stats["already_researched"] += 1
            elif not email_domain or is_free_mail_domain(email_domain):
                to_flag.append(email)
                stats["free_mail"] += 1
            elif not domain_delivers(email_domain):
                to_flag.append(email)
                stats["dead_domain"] += 1
            else:
                stats["kept"] += 1
        if to_remove:
            self.remove(to_remove)
        if to_flag:
            self.mark_dead(to_flag)
        logger.info(
            "PendingLeadsStore sweep: %d pending | kept=%d free_mail=%d "
            "dead_domain=%d already_researched=%d",
            stats["total"], stats["kept"], stats["free_mail"],
            stats["dead_domain"], stats["already_researched"],
        )
        return stats

    def remove(self, emails: list[str]) -> int:
        """Remove researched emails from pending; return count removed."""
        if not emails:
            return 0
        conn = self._conn()
        removed = 0
        for email in emails:
            eh = _email_hash(email)
            cur = conn.execute("DELETE FROM pending_leads WHERE email_hash = ?", (eh,))
            removed += cur.rowcount
        conn.commit()
        conn.close()
        return removed

    def count(self) -> int:
        conn = self._conn()
        n = conn.execute("SELECT COUNT(*) FROM pending_leads").fetchone()[0]
        conn.close()
        return n


class LeadResearchService:
    """High-level service: agent + store."""

    def __init__(
        self,
        *,
        store: LeadResearchStore | None = None,
        agent: AILeadResearchAgent | None = None,
    ) -> None:
        self.store = store or LeadResearchStore()
        if agent is None:
            # Deterministic query-yield loop persists beside the dossier store
            # (the runner's own DB), so the API/worker path learns per the same
            # crediting store as run_research. Enabled post-construction (bare
            # construction stays compatible with no-arg agent stubs in tests);
            # a stub store without ``_db_path`` just disables the loop.
            self.agent = AILeadResearchAgent()
            _yield_db = getattr(self.store, "_db_path", None)
            if _yield_db is not None and hasattr(self.agent, "enable_query_yield"):
                self.agent.enable_query_yield(_yield_db)
            if _yield_db is not None and hasattr(self.agent, "enable_fit_learning"):
                self.agent.enable_fit_learning(_yield_db)
        else:
            self.agent = agent

    def research(self, email: str, domain: str) -> LeadDossier:
        """Research one email+domain and persist the result."""
        dossier = self.agent.research(email, domain)
        self.store.save(dossier)
        return dossier

    def get(self, email: str) -> LeadDossier | None:
        return self.store.get(email)

    def list_leads(self) -> list[LeadDossier]:
        return self.store.list_all()

    def research_batch(self, records: list[dict[str, str]]) -> list[LeadDossier]:
        """Research a batch of {email, domain} records."""
        results = []
        for rec in records:
            email = rec.get("email", "")
            domain = rec.get("domain", "")
            if not email or not domain:
                continue
            try:
                dossier = self.research(email, domain)
                results.append(dossier)
            except Exception as exc:
                logger.error("Batch research failed for %s: %s", email, exc)
        return results
