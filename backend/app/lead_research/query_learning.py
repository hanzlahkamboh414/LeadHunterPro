"""Deterministic query-yield learning loop (CLAUDE.md §3/§7/§8/§11).

The old hardcoded query lists were right ONCE: numbers like "the BBB dork was
cited by 5 of 506 dossiers, so it is dropped" were a hand measurement baked
into the source. This module makes that judgment continuous and live, without
any extra AI call or provider credit.

Each search TEMPLATE (a stable label, not the concrete per-domain query) records
whether the URLs it returned ever became CITED evidence — and, of those, as
VERIFIED facts. After ``MIN_TRIALS`` real dispatched runs with zero verified
citations, the template is auto-dropped so no future lead spends a provider
credit on it. Until a template has enough data it is KEPT (default-keep is the
safe choice: dropping on one bad run would starve the pipeline).

Only new dossiers feed the loop (existing dossiers cannot be attributed to a
template retrospectively), so the first runs simply accumulate — pruning starts
no earlier than it has evidence for.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)

#: Dispatched runs before a zero-verified template is considered proven useless.
MIN_TRIALS = 12

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "..", "output", "lead_research.db")

#: Serializes writes across concurrent research threads (LEADS_CONCURRENCY).
_write_lock = threading.Lock()


class QueryYieldStore:
    """Persistent record of per-template citation yield.

    One small table in the lead-research DB (``query_template_yield``). Each row
    is a template label with:
      trials   — times the template was dispatched (search actually issued)
      cited    — of those, runs where >=1 returned URL was later cited in evidence
      verified — of those, runs where a returned URL was cited as "verified"
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        # A fresh connection per call: the store is shared across concurrent
        # research threads (run_research's pool), and reusing one connection
        # across threads is not safe in SQLite. Writes are serialized under the
        # module lock; reads are cheap.
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = self._conn()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_template_yield (
                    template TEXT PRIMARY KEY,
                    trials INTEGER NOT NULL DEFAULT 0,
                    cited INTEGER NOT NULL DEFAULT 0,
                    verified INTEGER NOT NULL DEFAULT 0,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def upsert(self, template: str, *, trials: int = 1, cited: int = 0, verified: int = 0) -> None:
        """Add one run's outcome to the template's cumulative counts."""
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO query_template_yield (template, trials, cited, verified, last_seen)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(template) DO UPDATE SET
                        trials = trials + excluded.trials,
                        cited = cited + excluded.cited,
                        verified = verified + excluded.verified,
                        last_seen = CURRENT_TIMESTAMP
                    """,
                    (template, trials, cited, verified),
                )
                conn.commit()
            finally:
                conn.close()

    def get(self, template: str) -> dict[str, int] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT trials, cited, verified FROM query_template_yield WHERE template = ?",
                (template,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {"trials": row[0], "cited": row[1], "verified": row[2]}

    def all(self) -> dict[str, dict[str, int]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT template, trials, cited, verified FROM query_template_yield"
            ).fetchall()
        finally:
            conn.close()
        return {r[0]: {"trials": r[1], "cited": r[2], "verified": r[3]} for r in rows}

    def should_skip(self, template: str) -> bool:
        """True to drop this template on the next run.

        Dropped only when it has enough real trials AND never once produced a
        verified citation. No record (template never run) → keep.
        """
        row = self.get(template)
        if row is None:
            return False
        return row["trials"] >= MIN_TRIALS and row["verified"] == 0

    def reset(self) -> None:
        """Test helper — clear all yield rows."""
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM query_template_yield")
                conn.commit()
            finally:
                conn.close()


class QueryYieldPlanner:
    """One research call's accumulator for the learn loop.

    Created fresh per ``agent.research()`` call (an agent is shared across
    concurrent threads, so per-call state must never live on the agent). The
    research stages call :meth:`note` as searches are issued; the agent calls
    :meth:`commit` once, after the dossier is complete, so counts are written
    exactly once per lead with full citation context.
    """

    def __init__(self, store: QueryYieldStore | None = None) -> None:
        self._store = store
        self._template_urls: dict[str, list[str]] = {}

    @property
    def enabled(self) -> bool:
        return self._store is not None

    def note(self, template: str, urls: list[str]) -> None:
        """Record the URLs a search template returned for this lead."""
        if not template or not urls:
            return
        seen = self._template_urls.setdefault(template, [])
        for u in urls:
            if u and u not in seen:
                seen.append(u)

    def should_skip(self, template: str) -> bool:
        """Ask the persistent loop whether to drop this template."""
        return bool(self._store) and self._store.should_skip(template)

    def commit(self, cited_urls: set[str], verified_urls: set[str]) -> None:
        """Persist every observed template's yield against the dossier evidence.

        ``cited_urls`` — all source_urls actually cited by the dossier.
        ``verified_urls`` — the subset cited with confidence ``verified``.
        A template is credited when one of its returned URLs appears in the
        cited (or verified) set.
        """
        if self._store is None or not self._template_urls:
            return
        for template, urls in self._template_urls.items():
            hit_cited = any(u in cited_urls for u in urls)
            hit_verified = any(u in verified_urls for u in urls)
            self._store.upsert(
                template,
                trials=1,
                cited=1 if hit_cited else 0,
                verified=1 if hit_verified else 0,
            )
        if logger.isEnabledFor(logging.INFO):
            total = len(self._template_urls)
            verified = sum(
                1 for u in self._template_urls.values() if any(x in verified_urls for x in u)
            )
            logger.info(
                "Query-yield recorded %d template(s), %d with a verified citation",
                total, verified,
            )