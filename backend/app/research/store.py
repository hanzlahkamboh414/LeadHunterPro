"""ResearchEvidenceStore — the research evidence engine's own DB.

One SQLite file per concern (the ``source_scout.db`` / ``harvester.db`` /
``phone_leads.db`` pattern). The data boundary is founder-frozen
(2026-09-18):

    dossiers.db           user/business dossier state  — NEVER touched here
    research_evidence.db  evidence, projects, runs     — this file
    source_scout.db       source registry + health     — not duplicated here

WHY A SEPARATE FILE (not 17 tables inside ``dossiers.db``): a dossier is
user/business state — folders, tags, CRM stage, next actions — and the repo
already treats it as unclobberable across a re-research. Evidence is
disposable, regenerable intelligence. Putting regenerable rows in the same
file as unclobberable user state means one bad migration risks the thing
that cannot be rebuilt. A separate file makes the blast radius a delete.

WHY COMPANY IDENTITY IS NOT THE DOMAIN (founder correction, 2026-09-18):
keying on the normalized domain would orphan every piece of evidence the
moment a company rebrands or operates two domains. So identity is a stable
generated ``company_id``, the domain is only a lookup handle
(``company_key``), and ``company_domains`` holds every domain ever seen for
that company with one flagged primary. A domain change therefore ADDS an
alias; nothing is orphaned.

Tables
------
``companies``        one row per company — the stable id + display name
``company_domains``  domain → company_id, primary flagged (aliases/history)
``evidence``         the canonical observations (see app/research/models.py)
``research_runs``    one row per research execution against a company. Its
                     ``state`` uses the founder-frozen four-value vocabulary
                     (``check.txt`` §31) so "found nothing" and "could not
                     look" are different stored facts, never collapsed.

Schema evolution: this store follows the repo's guarded-additive-ALTER
convention (``_add_column`` below mirrors ``JobStore._init_db`` and
``lead_research.service._add_column``). Those two remain their own private
copies; converging all three into one shared helper is a separate,
separately-approved refactor (CLAUDE.md §13 — no drive-by edits to shipped
code), and is recorded here so it is not forgotten.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.research.events.models import EventRecord
from app.research.models import CanonicalEvidence, CompanyIdentity
from app.research.outreach.models import OutreachTriggerRecord
from app.research.pain.models import PainHypothesisRecord, PainVerdict
from app.research.signals.models import SignalRecord
from app.research.taxonomy import ResearchState

_INIT_LOCK = threading.RLock()

#: How many domains one company may accumulate before linking is refused.
#: A real company has a handful; hundreds means a resolution bug upstream,
#: and silently accepting them would merge unrelated companies.
MAX_DOMAINS_PER_COMPANY = 25


def _now() -> str:
    """Current UTC timestamp, second precision (the repo's store convention)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def default_db_path() -> str:
    """The real output DB path (``backend/output/research_evidence.db``).

    Exposed so a caller can EXISTENCE-CHECK the file before constructing a
    store — a machine that never ran the engine must not get a DB created
    just because a module was imported.
    """
    return os.path.join(
        os.path.dirname(__file__), "..", "..", "output", "research_evidence.db",
    )


def _add_column(conn: sqlite3.Connection, table: str, name: str, decl: str) -> None:
    """Guarded additive ALTER — the repo's established migration pattern.

    TOCTOU-safe: a racing construction may have added the column between our
    PRAGMA check and our ALTER; "duplicate column name" then means the
    migration already happened, not a failure. Any OTHER OperationalError
    still raises — swallowing a real schema error would leave a column
    silently missing.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc):
            raise


def normalize_company_key(domain: str) -> str:
    """The lookup handle for a company: its normalized registrable host.

    Reuses :func:`app.email.pattern_inference.domain_of` (CLAUDE.md §14)
    rather than adding a fourth domain parser to the repo.

    Known limitation, stated rather than hidden: that helper strips a
    leading ``www.`` and lowercases, but does not reduce to eTLD+1 — so
    ``mail.acme.com`` stays ``mail.acme.com``. Being conservative is the
    safe direction here: two subdomains of one company stay separate
    (an extra company) instead of two unrelated companies being merged
    (corrupted evidence).
    """
    from app.email.pattern_inference import domain_of

    return domain_of(domain)


def new_company_id() -> str:
    """A fresh, stable company id — deliberately not derived from a domain."""
    return "cmp_" + uuid.uuid4().hex[:16]


class ResearchEvidenceStore:
    """SQLite persistence for canonical evidence, keyed by company."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or default_db_path()
        self._init_db()

    # -- plumbing ----------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        # The cross-process race is the same one lead_research.service
        # documents: two processes both see a missing column, both ALTER, the
        # loser dies on "duplicate column". The lock is the primary fix and
        # _add_column's tolerance is defence in depth.
        with _INIT_LOCK:
            parent = os.path.dirname(self._db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = self._conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS companies (
                    company_id TEXT PRIMARY KEY,
                    company_key TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            _add_column(conn, "companies", "pain_basis_hash", "TEXT NOT NULL DEFAULT ''")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_companies_key
                    ON companies (company_key)
            """)
            # Aliases/history. ``domain`` is the PRIMARY KEY, not
            # (company_id, domain): a domain belongs to exactly one company,
            # which is the invariant resolution depends on.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS company_domains (
                    domain TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_company_domains_company
                    ON company_domains (company_id)
            """)
            # UNIQUE(company_id, content_hash) is the dedup contract: the
            # same sentence re-read from the same page is ONE row, so a
            # re-research updates instead of accumulating duplicates.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT '',
                    publisher TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    excerpt TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    retrieved_at TEXT NOT NULL DEFAULT '',
                    company_match TEXT NOT NULL DEFAULT 'unknown',
                    evidence_type TEXT NOT NULL DEFAULT '',
                    project_key TEXT NOT NULL DEFAULT '',
                    event_candidate TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    legacy_type TEXT NOT NULL DEFAULT '',
                    verification TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    UNIQUE (company_id, content_hash)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evidence_company "
                "ON evidence (company_id, retrieved_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evidence_project "
                "ON evidence (company_id, project_key)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    project_key TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    evidence_ids TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_company "
                "ON events (company_id, occurred_at)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    strength TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    computed_at TEXT NOT NULL,
                    contradiction TEXT NOT NULL DEFAULT '[]',
                    event_ids TEXT NOT NULL DEFAULT '[]',
                    event_count INTEGER NOT NULL DEFAULT 0,
                    independent_source_count INTEGER NOT NULL DEFAULT 0,
                    recency TEXT NOT NULL DEFAULT 'background',
                    UNIQUE (company_id, signal_type)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signals_company "
                "ON signals (company_id, strength, signal_type)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_evidence (
                    signal_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    contribution TEXT NOT NULL DEFAULT 'support',
                    PRIMARY KEY (signal_id, evidence_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pain_hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    pain_type TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    licensed_confidence REAL NOT NULL,
                    verdict TEXT NOT NULL,
                    blocked_by TEXT NOT NULL DEFAULT '[]',
                    computed_at TEXT NOT NULL,
                    reasoning TEXT NOT NULL DEFAULT '',
                    evidence_ids TEXT NOT NULL DEFAULT '[]',
                    signal_ids TEXT NOT NULL DEFAULT '[]',
                    direct_evidence_ids TEXT NOT NULL DEFAULT '[]',
                    UNIQUE (company_id, pain_type)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pain_company "
                "ON pain_hypotheses (company_id, verdict, pain_type)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS outreach_triggers (
                    trigger_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL UNIQUE,
                    angle TEXT NOT NULL,
                    basis TEXT NOT NULL DEFAULT '',
                    strength TEXT NOT NULL,
                    wording TEXT NOT NULL,
                    computed_at TEXT NOT NULL,
                    evidence_ids TEXT NOT NULL DEFAULT '[]'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pain_evidence (
                    hypothesis_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'support',
                    PRIMARY KEY (hypothesis_id, evidence_id)
                )
            """)
            # ``state`` holds the founder-frozen four-value vocabulary plus
            # 'running'. A run that has not finished is honestly 'running',
            # never a premature NOT_FOUND.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'running',
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    coverage_json TEXT NOT NULL DEFAULT '{}',
                    honest_reason TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_runs_company "
                "ON research_runs (company_id, started_at)"
            )
            conn.commit()
            conn.close()

    # -- company identity --------------------------------------------------

    def resolve_company(self, domain: str, *, name: str = "") -> CompanyIdentity:
        """Find or create the company that owns *domain*.

        Resolution is by domain (the lookup handle); the returned identity
        carries the STABLE ``company_id``, so a caller that keeps only the
        id is unaffected by a later domain change.

        Args:
            domain: A URL or bare domain. Required — see Raises.
            name: Display name, applied only when creating a new company.
                An existing company's name is NOT overwritten by a caller
                who may know less than the record already does.

        Returns:
            The resolved :class:`CompanyIdentity`.

        Raises:
            ValueError: If *domain* is empty. Refusing beats inventing a
                company: keying a domainless lookup on the name would create
                a duplicate company on every call, and duplicates split
                evidence in exactly the way this store exists to prevent.
                Use :meth:`create_company` when there is genuinely no domain.
        """
        key = normalize_company_key(domain)
        if not key:
            raise ValueError(
                "resolve_company requires a domain — a domainless lookup "
                "would create a duplicate company per call; use "
                "create_company() for a company known only by name"
            )
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT company_id FROM company_domains WHERE domain = ?",
                (key,),
            ).fetchone()
            if row is not None:
                return self._identity(conn, row["company_id"])

            company_id = new_company_id()
            now = _now()
            conn.execute(
                "INSERT INTO companies (company_id, company_key, name, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (company_id, key, name or "", now, now),
            )
            conn.execute(
                "INSERT INTO company_domains (domain, company_id, "
                "is_primary, first_seen_at) VALUES (?, ?, 1, ?)",
                (key, company_id, now),
            )
            conn.commit()
            return self._identity(conn, company_id)
        finally:
            conn.close()

    def create_company(self, name: str) -> CompanyIdentity:
        """Create a company known only by name (no domain yet).

        Kept separate from :meth:`resolve_company` so a domainless company is
        an explicit choice. Such a company is NOT resolvable by key — it has
        none — so calling this twice with the same name yields two
        companies. That is honest: without a domain, nothing distinguishes
        "the same company again" from "a different company with the same
        name", and merging them would be the guess this store refuses.
        """
        conn = self._conn()
        try:
            company_id = new_company_id()
            now = _now()
            conn.execute(
                "INSERT INTO companies (company_id, company_key, name, "
                "created_at, updated_at) VALUES (?, '', ?, ?, ?)",
                (company_id, name or "", now, now),
            )
            conn.commit()
            return self._identity(conn, company_id)
        finally:
            conn.close()

    def link_domain(
        self, company_id: str, domain: str, *, primary: bool = False
    ) -> bool:
        """Attach another domain to an existing company.

        This is what makes a rebrand non-destructive: the company keeps its
        ``company_id`` and gains an alias instead of becoming a second
        company with half the evidence.

        Args:
            company_id: The company to attach to.
            domain: A URL or bare domain.
            primary: Also mark it the primary domain.

        Returns:
            True when a new alias was added; False when the domain was
            already known (to this company or another).

        Raises:
            ValueError: If the company is unknown, or it already holds
                :data:`MAX_DOMAINS_PER_COMPANY` domains — a runaway alias
                list means a resolution bug upstream, and accepting it
                silently would quietly merge unrelated companies.
        """
        key = normalize_company_key(domain)
        if not key:
            return False
        conn = self._conn()
        try:
            exists = conn.execute(
                "SELECT 1 FROM companies WHERE company_id = ?", (company_id,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"unknown company_id {company_id!r}")
            # Check the alias BEFORE the cap: re-asserting a domain the
            # company already holds is a no-op, not a cap violation, and a
            # caller that is at the cap must still get False rather than an
            # exception for a link that already exists.
            known = conn.execute(
                "SELECT company_id FROM company_domains WHERE domain = ?",
                (key,),
            ).fetchone()
            if known is not None:
                if known["company_id"] == company_id and primary:
                    self._set_primary(conn, company_id, key)
                    conn.commit()
                return False
            held = conn.execute(
                "SELECT COUNT(*) AS n FROM company_domains WHERE company_id = ?",
                (company_id,),
            ).fetchone()["n"]
            if held >= MAX_DOMAINS_PER_COMPANY:
                raise ValueError(
                    f"company {company_id!r} already holds {held} domains "
                    f"(max {MAX_DOMAINS_PER_COMPANY}) — refusing to merge more"
                )
            try:
                conn.execute(
                    "INSERT INTO company_domains (domain, company_id, "
                    "is_primary, first_seen_at) VALUES (?, ?, ?, ?)",
                    (key, company_id, 1 if primary else 0, _now()),
                )
            except sqlite3.IntegrityError:
                # Race defence: another process linked this domain between
                # the check above and this insert.
                conn.rollback()
                return False
            if primary:
                self._set_primary(conn, company_id, key)
            conn.execute(
                "UPDATE companies SET updated_at = ? WHERE company_id = ?",
                (_now(), company_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    @staticmethod
    def _set_primary(conn: sqlite3.Connection, company_id: str, domain: str) -> None:
        """Flag *domain* primary and demote every other domain of the company.

        Exactly one primary, enforced rather than assumed: without the demote
        step both rows stay flagged and "primary" stops meaning anything, so
        the ordering tiebreak silently falls back to insertion order.
        """
        conn.execute(
            "UPDATE company_domains SET is_primary = 1 "
            "WHERE company_id = ? AND domain = ?",
            (company_id, domain),
        )
        conn.execute(
            "UPDATE company_domains SET is_primary = 0 "
            "WHERE company_id = ? AND domain <> ?",
            (company_id, domain),
        )

    def get_company(self, company_id: str) -> CompanyIdentity | None:
        """The identity for *company_id*, or ``None`` when unknown."""
        conn = self._conn()
        try:
            return self._identity(conn, company_id)
        finally:
            conn.close()

    def set_name(self, company_id: str, name: str) -> bool:
        """Fill a company's display name when it has none yet.

        Phase 1 needs this: the planner resolves a company from a domain
        before it has read a page, so the name arrives later. Filling a blank
        is safe; OVERWRITING a name is not — a later caller may know less
        than the stored record, and a silent downgrade of the display name
        would be its own small version of the defect this project fixes. So
        an existing name is left alone and renaming is deliberately not
        exposed here.

        Args:
            company_id: The company to name.
            name: The display name. Blank is ignored.

        Returns:
            True when the name was set, False when it was already present or
            *name* is blank.

        Raises:
            ValueError: If the company is unknown.
        """
        cleaned = (name or "").strip()
        if not cleaned:
            return False
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT name FROM companies WHERE company_id = ?", (company_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown company_id {company_id!r}")
            if (row["name"] or "").strip():
                return False
            conn.execute(
                "UPDATE companies SET name = ?, updated_at = ? "
                "WHERE company_id = ?",
                (cleaned, _now(), company_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def companies_for_domain(self, domain: str) -> CompanyIdentity | None:
        """The company that owns *domain*, or ``None``."""
        key = normalize_company_key(domain)
        if not key:
            return None
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT company_id FROM company_domains WHERE domain = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            return self._identity(conn, row["company_id"])
        finally:
            conn.close()

    def _identity(
        self, conn: sqlite3.Connection, company_id: str
    ) -> CompanyIdentity | None:
        """Build a :class:`CompanyIdentity` from the row + its domains."""
        row = conn.execute(
            "SELECT company_id, company_key, name FROM companies "
            "WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        if row is None:
            return None
        domains = tuple(
            r["domain"]
            for r in conn.execute(
                "SELECT domain FROM company_domains WHERE company_id = ? "
                "ORDER BY is_primary DESC, first_seen_at ASC",
                (company_id,),
            )
        )
        return CompanyIdentity(
            company_id=row["company_id"],
            company_key=row["company_key"],
            name=row["name"],
            domains=domains,
        )

    # -- evidence ----------------------------------------------------------

    def add_evidence(self, evidence: CanonicalEvidence) -> bool:
        """Store one observation, deduplicated by content.

        Idempotent by construction: the same sentence from the same page is
        one row (``UNIQUE(company_id, content_hash)``), so re-research
        refines rather than duplicates (``check.txt`` §19).

        Args:
            evidence: The validated canonical record.

        Returns:
            True when a new row was written, False when it was already known.

        Raises:
            ValueError: If the record's ``company_id`` is unknown — evidence
                must attach to a real company identity, never float free —
                or if its ``evidence_id`` is already held by a DIFFERENT
                observation. That second case is not deduplication: the row
                would be dropped by the primary key and reported as "already
                known", silently losing a fact. Surface it instead.
        """
        row = evidence.to_row()
        conn = self._conn()
        try:
            known = conn.execute(
                "SELECT 1 FROM companies WHERE company_id = ?",
                (row["company_id"],),
            ).fetchone()
            if known is None:
                raise ValueError(
                    f"unknown company_id {row['company_id']!r} — resolve the "
                    f"company before storing evidence about it"
                )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO evidence (
                    evidence_id, company_id, source_url, source_type,
                    publisher, title, excerpt, published_at, retrieved_at,
                    company_match, evidence_type, project_key, event_candidate,
                    confidence, legacy_type, verification, content_hash
                ) VALUES (
                    :evidence_id, :company_id, :source_url, :source_type,
                    :publisher, :title, :excerpt, :published_at, :retrieved_at,
                    :company_match, :evidence_type, :project_key,
                    :event_candidate, :confidence, :legacy_type, :verification,
                    :content_hash
                )
                """,
                row,
            )
            if cursor.rowcount > 0:
                conn.commit()
                return True

            # The insert was ignored. Usually that is deduplication working:
            # the same observation was already stored. But OR IGNORE also
            # swallows a primary-key clash, so confirm which one this is
            # before reporting success-by-duplicate.
            existing = conn.execute(
                "SELECT company_id, content_hash FROM evidence "
                "WHERE evidence_id = ?",
                (row["evidence_id"],),
            ).fetchone()
            if existing is not None and (
                existing["company_id"] != row["company_id"]
                or existing["content_hash"] != row["content_hash"]
            ):
                raise ValueError(
                    f"evidence_id {row['evidence_id']!r} is already held by a "
                    f"different observation (company "
                    f"{existing['company_id']!r}) — evidence ids are derived "
                    f"from the observation; reusing one silently drops a fact"
                )
            conn.commit()
            return False
        finally:
            conn.close()

    def add_many(self, evidence: list[CanonicalEvidence]) -> int:
        """Store several observations; returns the count actually added."""
        return sum(1 for item in evidence if self.add_evidence(item))

    def evidence_for_company(
        self,
        company_id: str,
        *,
        project_key: str | None = None,
    ) -> list[CanonicalEvidence]:
        """Every observation for a company, newest-published first.

        Ordering note: rows with an EMPTY ``published_at`` sort last, not
        first. Undated evidence is not "newest" — it is undated, and sorting
        it to the top would let an undated claim lead a recency-ranked view.
        """
        conn = self._conn()
        try:
            sql = (
                "SELECT * FROM evidence WHERE company_id = ?"
            )
            params: list[Any] = [company_id]
            if project_key is not None:
                sql += " AND project_key = ?"
                params.append(project_key)
            sql += (
                " ORDER BY (published_at = '') ASC, published_at DESC, "
                "retrieved_at DESC"
            )
            return [
                CanonicalEvidence.from_row(dict(r))
                for r in conn.execute(sql, params)
            ]
        finally:
            conn.close()

    def count_evidence(self, company_id: str) -> int:
        """How many observations are stored for a company."""
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM evidence WHERE company_id = ?",
                (company_id,),
            ).fetchone()["n"]
        finally:
            conn.close()

    def project_keys(self, company_id: str) -> list[str]:
        """Distinct non-empty project keys seen for a company.

        Empty keys are excluded on purpose: "we could not identify a
        project" is not a project (``check.txt`` §19).
        """
        conn = self._conn()
        try:
            return [
                r["project_key"]
                for r in conn.execute(
                    "SELECT DISTINCT project_key FROM evidence "
                    "WHERE company_id = ? AND project_key <> '' "
                    "ORDER BY project_key",
                    (company_id,),
                )
            ]
        finally:
            conn.close()

    # -- events ------------------------------------------------------------

    def add_event(self, event: EventRecord) -> bool:
        """Store one validated event, refusing foreign or missing evidence."""
        conn = self._conn()
        try:
            company = conn.execute(
                "SELECT 1 FROM companies WHERE company_id = ?", (event.company_id,)
            ).fetchone()
            if company is None:
                raise ValueError(f"unknown company_id {event.company_id!r}")
            placeholders = ",".join("?" for _ in event.evidence_ids)
            rows = conn.execute(
                f"SELECT evidence_id, company_id FROM evidence "
                f"WHERE evidence_id IN ({placeholders})",
                event.evidence_ids,
            ).fetchall()
            found = {row["evidence_id"]: row["company_id"] for row in rows}
            missing = [value for value in event.evidence_ids if value not in found]
            if missing:
                raise ValueError(f"unknown evidence_id(s): {', '.join(missing)}")
            foreign = [value for value, owner in found.items() if owner != event.company_id]
            if foreign:
                raise ValueError("event evidence belongs to another company")
            row = event.to_row()
            row["evidence_ids"] = json.dumps(row["evidence_ids"])
            cursor = conn.execute(
                "INSERT OR IGNORE INTO events (event_id, company_id, event_type, "
                "project_key, occurred_at, confidence, evidence_ids) VALUES "
                "(:event_id, :company_id, :event_type, :project_key, :occurred_at, "
                ":confidence, :evidence_ids)",
                row,
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def events_for_company(self, company_id: str) -> list[EventRecord]:
        """Return stored events newest first, with undated events last."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE company_id = ? "
                "ORDER BY (occurred_at = '') ASC, occurred_at DESC, event_id",
                (company_id,),
            ).fetchall()
            return [
                EventRecord(
                    event_id=row["event_id"],
                    company_id=row["company_id"],
                    event_type=row["event_type"],
                    project_key=row["project_key"],
                    occurred_at=row["occurred_at"],
                    confidence=row["confidence"],
                    evidence_ids=tuple(json.loads(row["evidence_ids"])),
                )
                for row in rows
            ]
        finally:
            conn.close()

    # -- signals -----------------------------------------------------------

    def replace_signals(
        self,
        company_id: str,
        signals: list[SignalRecord] | tuple[SignalRecord, ...],
    ) -> None:
        """Atomically replace one company's computed signal snapshot."""
        snapshot = tuple(signals)
        if any(item.company_id != company_id for item in snapshot):
            raise ValueError("signal belongs to another company")
        conn = self._conn()
        try:
            company = conn.execute(
                "SELECT 1 FROM companies WHERE company_id = ?", (company_id,)
            ).fetchone()
            if company is None:
                raise ValueError(f"unknown company_id {company_id!r}")
            evidence_ids = tuple(dict.fromkeys(
                value for signal in snapshot for value in signal.evidence_ids
            ))
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                rows = conn.execute(
                    f"SELECT evidence_id, company_id FROM evidence "
                    f"WHERE evidence_id IN ({placeholders})",
                    evidence_ids,
                ).fetchall()
                found = {row["evidence_id"]: row["company_id"] for row in rows}
                missing = [value for value in evidence_ids if value not in found]
                if missing:
                    raise ValueError(f"unknown evidence_id(s): {', '.join(missing)}")
                if any(owner != company_id for owner in found.values()):
                    raise ValueError("signal evidence belongs to another company")

            old_ids = [
                row["signal_id"] for row in conn.execute(
                    "SELECT signal_id FROM signals WHERE company_id = ?", (company_id,)
                )
            ]
            if old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                conn.execute(
                    f"DELETE FROM signal_evidence WHERE signal_id IN ({placeholders})",
                    old_ids,
                )
            conn.execute("DELETE FROM signals WHERE company_id = ?", (company_id,))
            for signal in snapshot:
                row = signal.to_row()
                row["contradiction"] = json.dumps(row["contradiction"])
                row["event_ids"] = json.dumps(row["event_ids"])
                conn.execute(
                    "INSERT INTO signals (signal_id, company_id, signal_type, "
                    "strength, score, computed_at, contradiction, event_ids, "
                    "event_count, independent_source_count, recency) VALUES "
                    "(:signal_id, :company_id, :signal_type, :strength, :score, "
                    ":computed_at, :contradiction, :event_ids, :event_count, "
                    ":independent_source_count, :recency)",
                    row,
                )
                conn.executemany(
                    "INSERT INTO signal_evidence "
                    "(signal_id, evidence_id, contribution) VALUES (?, ?, 'support')",
                    [(signal.signal_id, value) for value in signal.evidence_ids],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def signals_for_company(self, company_id: str) -> list[SignalRecord]:
        """Return the latest deterministic signal snapshot for a company."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM signals WHERE company_id = ? "
                "ORDER BY score DESC, signal_type",
                (company_id,),
            ).fetchall()
            records: list[SignalRecord] = []
            for row in rows:
                evidence_ids = tuple(
                    item["evidence_id"] for item in conn.execute(
                        "SELECT evidence_id FROM signal_evidence "
                        "WHERE signal_id = ? ORDER BY rowid",
                        (row["signal_id"],),
                    )
                )
                records.append(SignalRecord(
                    signal_id=row["signal_id"],
                    company_id=row["company_id"],
                    signal_type=row["signal_type"],
                    strength=row["strength"],
                    score=row["score"],
                    computed_at=row["computed_at"],
                    contradiction=tuple(json.loads(row["contradiction"])),
                    evidence_ids=evidence_ids,
                    event_ids=tuple(json.loads(row["event_ids"])),
                    event_count=row["event_count"],
                    independent_source_count=row["independent_source_count"],
                    recency=row["recency"],
                ))
            return records
        finally:
            conn.close()

    # -- pain hypotheses ---------------------------------------------------

    def replace_pain_hypotheses(
        self,
        company_id: str,
        hypotheses: list[PainHypothesisRecord] | tuple[PainHypothesisRecord, ...],
        *,
        basis_hash: str = "",
    ) -> None:
        """Atomically replace one company's gated pain snapshot."""
        snapshot = tuple(hypotheses)
        if any(item.company_id != company_id for item in snapshot):
            raise ValueError("pain hypothesis belongs to another company")
        conn = self._conn()
        try:
            company = conn.execute(
                "SELECT 1 FROM companies WHERE company_id = ?", (company_id,)
            ).fetchone()
            if company is None:
                raise ValueError(f"unknown company_id {company_id!r}")
            evidence_ids = tuple(dict.fromkeys(
                value for item in snapshot for value in item.evidence_ids
            ))
            found: dict[str, str] = {}
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                rows = conn.execute(
                    f"SELECT evidence_id, company_id FROM evidence "
                    f"WHERE evidence_id IN ({placeholders})",
                    evidence_ids,
                ).fetchall()
                found = {row["evidence_id"]: row["company_id"] for row in rows}
                if any(owner != company_id for owner in found.values()):
                    raise ValueError("pain evidence belongs to another company")
                missing = [value for value in evidence_ids if value not in found]
                unsafe_missing = [
                    value for value in missing
                    if any(
                        value in item.evidence_ids
                        and item.verdict is not PainVerdict.BLOCKED
                        for item in snapshot
                    )
                ]
                if unsafe_missing:
                    raise ValueError(
                        f"unknown evidence_id(s): {', '.join(unsafe_missing)}"
                    )

            old_ids = [
                row["hypothesis_id"] for row in conn.execute(
                    "SELECT hypothesis_id FROM pain_hypotheses WHERE company_id = ?",
                    (company_id,),
                )
            ]
            if old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                conn.execute(
                    f"DELETE FROM pain_evidence WHERE hypothesis_id IN ({placeholders})",
                    old_ids,
                )
            conn.execute(
                "DELETE FROM pain_hypotheses WHERE company_id = ?", (company_id,)
            )
            for hypothesis in snapshot:
                row = hypothesis.to_row()
                for name in (
                    "blocked_by", "evidence_ids", "signal_ids", "direct_evidence_ids",
                ):
                    row[name] = json.dumps(row[name])
                conn.execute(
                    "INSERT INTO pain_hypotheses (hypothesis_id, company_id, "
                    "pain_type, confidence, licensed_confidence, verdict, blocked_by, "
                    "computed_at, reasoning, evidence_ids, signal_ids, "
                    "direct_evidence_ids) VALUES (:hypothesis_id, :company_id, "
                    ":pain_type, :confidence, :licensed_confidence, :verdict, "
                    ":blocked_by, :computed_at, :reasoning, :evidence_ids, "
                    ":signal_ids, :direct_evidence_ids)",
                    row,
                )
                conn.executemany(
                    "INSERT INTO pain_evidence (hypothesis_id, evidence_id, role) "
                    "VALUES (?, ?, ?)",
                    [
                        (
                            hypothesis.hypothesis_id,
                            value,
                            "direct" if value in hypothesis.direct_evidence_ids else "support",
                        )
                        for value in hypothesis.evidence_ids if value in found
                    ],
                )
            conn.execute(
                "UPDATE companies SET pain_basis_hash = ?, updated_at = ? "
                "WHERE company_id = ?",
                ((basis_hash or "").strip(), _now(), company_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def pain_hypotheses_for_company(
        self, company_id: str
    ) -> list[PainHypothesisRecord]:
        """Return the latest gated pain snapshot for a company."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM pain_hypotheses WHERE company_id = ? "
                "ORDER BY confidence DESC, pain_type",
                (company_id,),
            ).fetchall()
            return [
                PainHypothesisRecord(
                    hypothesis_id=row["hypothesis_id"],
                    company_id=row["company_id"],
                    pain_type=row["pain_type"],
                    confidence=row["confidence"],
                    licensed_confidence=row["licensed_confidence"],
                    verdict=row["verdict"],
                    blocked_by=tuple(json.loads(row["blocked_by"])),
                    computed_at=row["computed_at"],
                    reasoning=row["reasoning"],
                    evidence_ids=tuple(json.loads(row["evidence_ids"])),
                    signal_ids=tuple(json.loads(row["signal_ids"])),
                    direct_evidence_ids=tuple(json.loads(row["direct_evidence_ids"])),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def pain_basis_hash(self, company_id: str) -> str:
        """Hash of the signal snapshot last sent through AI Call #2."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT pain_basis_hash FROM companies WHERE company_id = ?",
                (company_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown company_id {company_id!r}")
            return row["pain_basis_hash"] or ""
        finally:
            conn.close()

    # -- outreach triggers -------------------------------------------------

    def replace_outreach_trigger(
        self, company_id: str, trigger: OutreachTriggerRecord
    ) -> None:
        """Replace one company's licensed outreach trigger atomically."""
        if trigger.company_id != company_id:
            raise ValueError("outreach trigger belongs to another company")
        conn = self._conn()
        try:
            company = conn.execute(
                "SELECT 1 FROM companies WHERE company_id = ?", (company_id,)
            ).fetchone()
            if company is None:
                raise ValueError(f"unknown company_id {company_id!r}")
            if trigger.evidence_ids:
                placeholders = ",".join("?" for _ in trigger.evidence_ids)
                rows = conn.execute(
                    f"SELECT evidence_id, company_id FROM evidence "
                    f"WHERE evidence_id IN ({placeholders})",
                    trigger.evidence_ids,
                ).fetchall()
                found = {row["evidence_id"]: row["company_id"] for row in rows}
                missing = [item for item in trigger.evidence_ids if item not in found]
                if missing:
                    raise ValueError(f"unknown evidence_id(s): {', '.join(missing)}")
                if any(owner != company_id for owner in found.values()):
                    raise ValueError("outreach evidence belongs to another company")
            conn.execute("DELETE FROM outreach_triggers WHERE company_id = ?", (company_id,))
            row = trigger.to_dict()
            row["evidence_ids"] = json.dumps(row["evidence_ids"])
            conn.execute(
                "INSERT INTO outreach_triggers (trigger_id, company_id, angle, basis, "
                "strength, wording, computed_at, evidence_ids) VALUES "
                "(:trigger_id, :company_id, :angle, :basis, :strength, :wording, "
                ":computed_at, :evidence_ids)",
                row,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def outreach_trigger_for_company(
        self, company_id: str
    ) -> OutreachTriggerRecord | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM outreach_triggers WHERE company_id = ?", (company_id,)
            ).fetchone()
            if row is None:
                return None
            return OutreachTriggerRecord(
                trigger_id=row["trigger_id"], company_id=row["company_id"],
                angle=row["angle"], basis=row["basis"], strength=row["strength"],
                wording=row["wording"], computed_at=row["computed_at"],
                evidence_ids=tuple(json.loads(row["evidence_ids"])),
            )
        finally:
            conn.close()

    # -- research runs -----------------------------------------------------

    def start_run(self, company_id: str) -> str:
        """Open a research run. Returns its id.

        The row starts in ``running`` — explicitly NOT one of the four
        honest outcome states — so an interrupted run can never be misread
        as "we searched and found nothing".
        """
        run_id = "run_" + uuid.uuid4().hex[:16]
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO research_runs (run_id, company_id, state, "
                "started_at) VALUES (?, ?, 'running', ?)",
                (run_id, company_id, _now()),
            )
            conn.commit()
            return run_id
        finally:
            conn.close()

    def finish_run(
        self,
        run_id: str,
        state: str,
        *,
        honest_reason: str = "",
        coverage: dict[str, Any] | None = None,
    ) -> None:
        """Close a run with one of the four honest states (§31).

        Args:
            run_id: The run to close.
            state: A :class:`~app.research.taxonomy.ResearchState` value.
                ``NOT_FOUND`` and ``NOT_ACCESSIBLE`` are different facts and
                the store does not normalize between them.
            honest_reason: Why, in words. Required for anything other than
                ``VERIFIED`` — a negative with no reason is the "live=False"
                logging failure CLAUDE.md §6 forbids.
            coverage: Per-bucket coverage, stored as JSON.

        Raises:
            ValueError: On an unknown state, or a non-VERIFIED state with no
                reason.
        """
        try:
            normalized = ResearchState(state).value
        except ValueError as exc:
            raise ValueError(
                f"unknown research state {state!r} — must be one of "
                f"{[s.value for s in ResearchState]}"
            ) from exc
        if normalized != ResearchState.VERIFIED.value and not honest_reason.strip():
            raise ValueError(
                f"state {normalized} requires an honest_reason — a negative "
                f"result with no reason is not diagnostic (CLAUDE.md §6)"
            )
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE research_runs SET state = ?, finished_at = ?, "
                "coverage_json = ?, honest_reason = ? WHERE run_id = ?",
                (
                    normalized,
                    _now(),
                    json.dumps(coverage or {}, sort_keys=True),
                    honest_reason,
                    run_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """One run row as a dict, or ``None``."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def latest_coverage_for_company(self, company_id: str) -> dict[str, Any]:
        """Decoded coverage from the company's latest completed research run."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT coverage_json FROM research_runs WHERE company_id = ? "
                "AND state != 'running' ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (company_id,),
            ).fetchone()
            if row is None:
                return {}
            value = json.loads(row["coverage_json"] or "{}")
            return value if isinstance(value, dict) else {}
        finally:
            conn.close()
