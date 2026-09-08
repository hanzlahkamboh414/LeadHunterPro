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
from typing import Any

from app.lead_research.agent import AILeadResearchAgent, DEAD_DOMAIN_MARKER
from app.lead_research.models import LeadDossier, LeadMeta

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


class LeadResearchStore:
    """SQLite store for LeadDossier results."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            import os
            db_path = os.path.join(os.path.dirname(__file__), "..", "..", "output", "lead_research.db")
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        import os
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
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
        # live DBs predate these columns, so add them guarded by PRAGMA and
        # never touch dossier_json. `save()`'s upsert only updates
        # dossier_json+updated_at on conflict, so these ride along untouched and
        # survive a pipeline re-research (user metadata is never clobbered).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(dossiers)")}
        if "folder" not in cols:
            conn.execute("ALTER TABLE dossiers ADD COLUMN folder TEXT NOT NULL DEFAULT ''")
        if "tags" not in cols:
            conn.execute("ALTER TABLE dossiers ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        # Phase B.2 — first-class folder catalog. A folder is a persisted, clickable
        # group (empty folders included — "create the folder first, then move leads").
        # Names are also backfilled from dossiers on read, so folders that only ever
        # existed as values on leads self-heal into the catalog (no data loss).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                name TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
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
        conn.commit()
        conn.close()

    def save(self, dossier: LeadDossier) -> None:
        """Save or update a dossier."""
        eh = _email_hash(dossier.email)
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            INSERT INTO dossiers (email_hash, email, domain, dossier_json, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(email_hash) DO UPDATE SET
                dossier_json = excluded.dossier_json,
                updated_at = CURRENT_TIMESTAMP
        """, (eh, dossier.email, dossier.domain, json.dumps(dossier.to_dict())))
        conn.commit()
        conn.close()

    def get(self, email: str) -> LeadDossier | None:
        """Retrieve a dossier by email."""
        eh = _email_hash(email)
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT dossier_json FROM dossiers WHERE email_hash = ?", (eh,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return LeadDossier.from_dict(json.loads(row[0]))

    def list_all(self) -> list[LeadDossier]:
        """List all stored dossiers."""
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            "SELECT dossier_json FROM dossiers ORDER BY updated_at DESC"
        ).fetchall()
        conn.close()
        return [LeadDossier.from_dict(json.loads(r[0])) for r in rows]

    def count(self) -> int:
        conn = sqlite3.connect(self._db_path)
        n = conn.execute("SELECT COUNT(*) FROM dossiers").fetchone()[0]
        conn.close()
        return n

    def delete(self, email: str, *, reason: str = "manual") -> bool:
        """Delete one dossier by email; return True if it existed.

        ``reason`` records WHY (``manual`` = per-lead Delete, ``junk`` = the
        Junk sweep) into the ``deleted_leads`` audit trail the admin screen
        reads ("kon kon c email delete ki"). Same transaction, so the log can
        never show a deletion the dossier survived (or vice versa).
        """
        eh = _email_hash(email)
        conn = sqlite3.connect(self._db_path)
        cur = conn.execute("DELETE FROM dossiers WHERE email_hash = ?", (eh,))
        if cur.rowcount > 0:
            conn.execute(
                "INSERT INTO deleted_leads (email, deleted_at, reason) "
                "VALUES (?, CURRENT_TIMESTAMP, ?) "
                "ON CONFLICT(email) DO UPDATE SET deleted_at = CURRENT_TIMESTAMP, reason = excluded.reason",
                (email, reason or "manual"),
            )
            conn.commit()
        conn.close()
        return cur.rowcount > 0

    def deleted_log(self, limit: int = 100) -> list[dict[str, str]]:
        """The admin audit trail: emails deleted, when, and why (newest first)."""
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            "SELECT email, deleted_at, reason FROM deleted_leads "
            "ORDER BY deleted_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        conn.close()
        return [
            {"email": r[0], "deleted_at": r[1], "reason": r[2]} for r in rows
        ]

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
        conn = sqlite3.connect(self._db_path)
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
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT folder, tags FROM dossiers WHERE email_hash = ?", (eh,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return LeadMeta.from_db(row[0], row[1])

    def all_meta(self) -> dict[str, LeadMeta]:
        """Map email_hash -> folder+tags for every stored dossier (one query)."""
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute("SELECT email_hash, folder, tags FROM dossiers").fetchall()
        conn.close()
        return {r[0]: LeadMeta.from_db(r[1], r[2]) for r in rows}

    def research_dates(self) -> dict[str, str]:
        """Map email_hash -> the research date (``YYYY-MM-DD``) of each dossier.

        The date a lead was first researched/persisted (``created_at``) is the
        honest "kis tareekh ko nikala" the list/detail views surface — never a
        rebuilt timestamp, the actual row the pipeline wrote.
        """
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            "SELECT email_hash, date(created_at) FROM dossiers"
        ).fetchall()
        conn.close()
        return {r[0]: (r[1] or "") for r in rows}

    # -- Folders catalog (Phase B.2) ------------------------------------

    @staticmethod
    def _backfill_folders(conn: sqlite3.Connection) -> None:
        """Self-heal the catalog with folder names used on dossiers.

        Keeps the store consistent for legacy leads (folder values that pre-date
        the catalog) AND for a rename/clear that moves leads — every real folder
        name ends up present as a clickable group, empty or not.
        """
        conn.execute(
            "INSERT OR IGNORE INTO folders(name) "
            "SELECT DISTINCT folder FROM dossiers WHERE folder <> ''"
        )

    def create_folder(self, name: str) -> bool:
        """Create a persisted (possibly empty) folder; True when newly created.

        The Phase B.2 contract: a folder exists FIRST (``"Monday data"``), is
        clickable in the filter even with zero leads, and fills as the user
        moves leads into it. Duplicate names are ignored (idempotent).
        """
        name = (name or "").strip()
        if not name:
            return False
        conn = sqlite3.connect(self._db_path)
        cur = conn.execute(
            "INSERT OR IGNORE INTO folders(name) VALUES (?)", (name,)
        )
        conn.commit()
        conn.close()
        return cur.rowcount == 1

    def list_folders(self) -> list[dict[str, Any]]:
        """Every catalog folder with its live lead count, newest first.

        Returns ``[{name, created_at, count}]`` — empty folders included (that
        is the point of the catalog). ``_backfill_folders`` makes sure a folder
        name that only rides on leads (legacy or post-rename) is never missing.
        """
        conn = sqlite3.connect(self._db_path)
        self._backfill_folders(conn)
        rows = conn.execute(
            "SELECT f.name, f.created_at, "
            "(SELECT COUNT(*) FROM dossiers d WHERE d.folder = f.name) AS cnt "
            "FROM folders f "
            "ORDER BY f.created_at DESC, f.name COLLATE NOCASE ASC"
        ).fetchall()
        conn.commit()  # backfill may have inserted rows
        conn.close()
        return [
            {"name": r[0], "created_at": (r[1] or "")[:19], "count": r[2]}
            for r in rows
        ]

    def folder_catalog(self) -> dict[str, Any]:
        """The organization mailbox overview — one round-trip for the UI.

        ``{folders: [{name, created_at, count}], unfiled, total}``: ``unfiled``
        is what the DEFAULT Companies view shows (leads still in the inbox,
        ``folder = ''``); ``total`` counts every dossier (folders included), so
        the "All" chip and Dashboard totals stay honest. Same self-healing
        backfill as :meth:`list_folders`.

        COUNTS = THE LIST, NOT THE STORE — the root cause of the "chip says
        313 but the view shows 38" bug. The leads list hides ``skip``/junk
        dossiers by default (a dead-domain / non-construction / sub-threshold
        dossier is not a lead), so the catalog applies the SAME re-gate
        (:func:`~app.lead_research.scoring.regate_recommendation`) and counts
        only what the list would actually show. Junk still exists in the store
        (purgeable via "Clear junk") — it is just never counted as a lead.
        """
        from app.lead_research.scoring import regate_recommendation

        conn = sqlite3.connect(self._db_path)
        self._backfill_folders(conn)
        rows = conn.execute(
            "SELECT f.name, f.created_at FROM folders f "
            "ORDER BY f.created_at DESC, f.name COLLATE NOCASE ASC"
        ).fetchall()
        conn.commit()  # backfill may have inserted rows
        conn.close()

        meta = self.all_meta()
        total = 0
        unfiled = 0
        by_folder: dict[str, int] = {}
        for d in self.list_all():
            if regate_recommendation(d) == "skip":
                continue  # hidden junk — counted nowhere (matches the list view)
            total += 1
            m = meta.get(_email_hash(d.email))
            f = m.folder if m else ""
            if f:
                by_folder[f] = by_folder.get(f, 0) + 1
            else:
                unfiled += 1
        return {
            "folders": [
                {"name": name, "created_at": (created or "")[:19], "count": by_folder.get(name, 0)}
                for name, created in rows
            ],
            "unfiled": unfiled,
            "total": total,
        }

    def rename_folder(self, old: str, new: str) -> int:
        """Rename a folder across every dossier AND the catalog; count renamed.

        The sweep is unchanged (folder rides on dossiers); the catalog keeps the
        group first-class. An empty folder that is renamed stays an empty group
        under the new name — nothing is lost.
        """
        old = (old or "").strip()
        new = (new or "").strip()
        if not old or old == new:
            return 0
        affected = [eh for eh, m in self.all_meta().items() if m.folder == old]
        for eh in affected:
            self.set_meta_direct(eh, new, None)
        conn = sqlite3.connect(self._db_path)
        conn.execute("DELETE FROM folders WHERE name = ?", (old,))
        conn.execute("INSERT OR IGNORE INTO folders(name) VALUES (?)", (new,))
        conn.commit()
        conn.close()
        return len(affected)

    def rename_tag(self, old: str, new: str) -> int:
        """Rename a tag value across every dossier; return how many changed."""
        old = (old or "").strip()
        new = (new or "").strip()
        if not old or old == new:
            return 0
        affected = 0
        for eh, m in self.all_meta().items():
            if old in m.tags:
                tags = [(new if t == old else t) for t in m.tags]
                self.set_meta_direct(eh, m.folder, tags)
                affected += 1
        return affected

    def clear_folder(self, value: str) -> int:
        """Remove a folder value from every dossier AND drop it from the catalog.

        Deleting a folder releases its leads (folder="") and removes the group —
        the honest count of released leads is returned (0 for an empty folder is
        a valid "folder deleted, nothing to release").
        """
        value = (value or "").strip()
        if not value:
            return 0
        affected = [eh for eh, m in self.all_meta().items() if m.folder == value]
        for eh in affected:
            self.set_meta_direct(eh, "", None)
        conn = sqlite3.connect(self._db_path)
        conn.execute("DELETE FROM folders WHERE name = ?", (value,))
        conn.commit()
        conn.close()
        return len(affected)

    def clear_tag(self, value: str) -> int:
        """Remove a tag value from every dossier; return how many changed."""
        value = (value or "").strip()
        if not value:
            return 0
        affected = 0
        for eh, m in self.all_meta().items():
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
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                "SELECT tags FROM dossiers WHERE email_hash = ?", (email_hash,)
            ).fetchone()
            conn.close()
            tags = list(LeadMeta.from_db("", row[0]).tags) if row else []
        _, tags_json = self._normalize(folder, tags)
        conn = sqlite3.connect(self._db_path)
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

    def _init_db(self) -> None:
        import os
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
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
        cols = {row[1] for row in conn.execute("PRAGMA table_info(pending_leads)")}
        if "dead" not in cols:
            conn.execute(
                "ALTER TABLE pending_leads ADD COLUMN dead INTEGER NOT NULL DEFAULT 0"
            )
        if "attempted_at" not in cols:
            conn.execute("ALTER TABLE pending_leads ADD COLUMN attempted_at TIMESTAMP")
        if "attempt_count" not in cols:
            conn.execute(
                "ALTER TABLE pending_leads ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()
        conn.close()

    def get(self, email: str) -> dict | None:
        eh = _email_hash(email)
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT email, domain, company, person, source_url, location "
            "FROM pending_leads WHERE email_hash = ?", (eh,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "email": row[0], "domain": row[1], "company": row[2],
            "person": row[3], "source_url": row[4], "location": row[5],
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

        conn = sqlite3.connect(self._db_path)
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
            conn.execute("""
                INSERT INTO pending_leads
                    (email_hash, email, domain, company, person, source_url, location)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email_hash) DO UPDATE SET location = excluded.location
            """, (
                eh, email,
                lead.get("domain", ""),
                lead.get("company", ""),
                lead.get("person", ""),
                lead.get("source_url", ""),
                lead.get("location", ""),
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
        conn = sqlite3.connect(self._db_path)
        if location:
            sql = (
                "SELECT email, domain, company, person, source_url, location "
                "FROM pending_leads WHERE location = ? AND dead = 0"
            )
            args: list[Any] = [location]
        else:
            sql = (
                "SELECT email, domain, company, person, source_url, location "
                "FROM pending_leads WHERE dead = 0"
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
        conn.close()
        # Same vertical gate as ``add``, applied at SERVE time too: rows cached
        # BEFORE the boundary existed (the DCTA transit/mobility junk) must never
        # be served again — they stay in the table (Phase C can purge to show
        # the user exactly what was excluded) but no research credit touches them.
        from app.company_profile import get_profile

        served: list[dict] = []
        for r in rows:
            if get_profile().lead_is_non_client(
                company=r[2], domain=r[1], source_url=r[4]
            ):
                continue
            served.append(
                {"email": r[0], "domain": r[1], "company": r[2],
                 "person": r[3], "source_url": r[4], "location": r[5]}
            )
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
        conn = sqlite3.connect(self._db_path)
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
        conn = sqlite3.connect(self._db_path)
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
        conn = sqlite3.connect(self._db_path)
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
        conn = sqlite3.connect(self._db_path)
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

        conn = sqlite3.connect(self._db_path)
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
        conn = sqlite3.connect(self._db_path)
        removed = 0
        for email in emails:
            eh = _email_hash(email)
            cur = conn.execute("DELETE FROM pending_leads WHERE email_hash = ?", (eh,))
            removed += cur.rowcount
        conn.commit()
        conn.close()
        return removed

    def count(self) -> int:
        conn = sqlite3.connect(self._db_path)
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
