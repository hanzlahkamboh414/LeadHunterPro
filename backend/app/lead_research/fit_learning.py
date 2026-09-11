"""Deterministic fit-learning loop (CLAUDE.md §3/§7/§8/§11).

The company-profile term list (``company_profile.py``) is a hand-written
BACKSTOP — it can only reject the off-vertical classes we already thought of.
This module makes the boundary LEARN from the pipeline's own verdicts, with no
extra AI call or provider credit:

* ``industry`` — after Stage-1 research the AI labels the company's true
  industry and judges client-fit. Each completed dossier records, per industry
  label, whether it ended as a REAL lead (contact_now / nurture) or was skipped.
  Once an industry has ``MIN_TRIALS`` completed runs and NEVER once produced a
  real lead, it is auto-skipped straight after Stage 1 — the expensive person /
  deep / intent / scoring lanes never run for a class the AI has already proven,
  by its own repeated judgement, is not a buyer. This is the "kabi wahi ghalti
  na kare" loop: the model teaches the deterministic gate.

* ``source`` — a discovery source host (a plan-holder site, a procurement list).
  Each researched lead records, per source host, whether it became a real lead.
  A source that has surfaced ``MIN_TRIALS`` researched leads and NEVER once
  produced one is auto-pruned at DISCOVERY time (before any credit is spent) —
  the general, self-correcting form of the hand-seeded non-client-source list
  (root cause of the 54-lead DCTA transit-vendor flood).

* ``company`` / ``domain`` — USER verdicts, not research outcomes. When the
  user deletes a dossier as ``irrelevant`` / ``not our client``, the company
  name and mail domain are recorded as USER-REJECTED. This is DECISIVE: one
  human rejection outweighs any number of AI scores, so ``user_rejects >= 1``
  skips that company/domain on the next discovery pass — the concrete fix for
  the 2026-09-08 purge (24 confirmed-irrelevant dossiers whose companies would
  otherwise resurface and burn credit again). Unlike ``industry``/``source``
  there is NO ``MIN_TRIALS`` and NO AI self-served credit on these namespaces:
  only the user's own delete verdict teaches them.

Default-KEEP is the safe choice everywhere: a label/host with too little data,
or any single real lead in its history, is never skipped. Pruning starts only
once there is enough evidence for it — exactly like the query-yield loop this
module mirrors (:mod:`app.lead_research.query_learning`).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading

logger = logging.getLogger(__name__)

#: Completed runs before a never-converting label/host is treated as proven junk.
MIN_TRIALS = 12

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "..", "output", "lead_research.db")

#: Serializes writes across concurrent research threads (LEADS_CONCURRENCY).
_write_lock = threading.Lock()

#: The four learning namespaces. A namespaced key keeps one table honest for all.
#: ``industry`` and ``source`` learn from the pipeline's own research verdicts
#: (credit after MIN_TRIALS); ``company``/``domain`` learn ONLY from explicit
#: user rejections (one verdict is decisive — no trial count applies).
KIND_INDUSTRY = "industry"
KIND_SOURCE = "source"
KIND_COMPANY = "company"
KIND_DOMAIN = "domain"


def normalize_industry(industry: str) -> str:
    """Collapse an industry label to a stable learning key.

    Lower-cased, whitespace-collapsed, capped. NOT split on ``/``/``-`` — a
    compound label like "General Contractor / Developer" is a distinct class
    from "General Contractor" and merging them would let one converting class
    mask a non-converting one. Empty stays empty (never learned).
    """
    return re.sub(r"\s+", " ", (industry or "").strip().lower())[:80]


def source_host(source_url: str) -> str:
    """The bare host of a discovery source URL, lower-cased (learning key).

    ``https://www.dcta.net/sites/x.pdf`` -> ``dcta.net``. A leading ``www.`` is
    dropped so ``www.x`` and ``x`` learn as one host. Empty/opaque -> "".
    """
    s = (source_url or "").strip().lower()
    if not s:
        return ""
    m = re.match(r"[a-z][a-z0-9+.\-]*://([^/]+)", s)
    host = (m.group(1) if m else s.split("/")[0]).split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def normalize_company(company_name: str) -> str:
    """Collapse a company name to a stable learning key.

    Lower-cased, whitespace-collapsed, boundary punctuation trimmed, capped.
    Legal-suffix words (LLC / Inc / Corp ...) are NOT stripped: "Acme" and
    "Acme Construction" stay distinct keys so one firm's rejection can never
    silently hide a different, valid lead that merely shares a first word.
    Empty stays empty (never learned).
    """
    s = re.sub(r"\s+", " ", (company_name or "").strip().lower())
    s = s.strip(" .,-#/\\&'\"()%")
    return s[:120]


def mail_domain(domain: str) -> str:
    """The bare lowercase mail domain — the learning key for a rejected domain.

    ``WWW.Acme.Com`` / ``acme.com`` / ``@acme.com`` -> ``acme.com``. A leading
    ``www.`` and stray ``@``/dots are dropped so one host is never learned as
    several spellings. Empty stays empty (never learned).
    """
    d = (domain or "").strip().lower()
    d = d.lstrip("@").strip()
    if d.startswith("www."):
        d = d[4:]
    return d.rstrip(".").strip()


class FitLearningStore:
    """Persistent per-(kind,key) record of how often a class became a real lead.

    One small table in the lead-research DB (``fit_learning``). Each row:
      trials — completed research runs attributed to this label/host
      kept   — of those, runs that ended as a REAL lead (not skipped)
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        # Fresh connection per call: the store is shared across concurrent
        # research threads and reusing one connection across threads is unsafe
        # in SQLite. Writes are serialized under the module lock; reads are cheap.
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = self._conn()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fit_learning (
                    kind TEXT NOT NULL,
                    key TEXT NOT NULL,
                    trials INTEGER NOT NULL DEFAULT 0,
                    kept INTEGER NOT NULL DEFAULT 0,
                    user_rejects INTEGER NOT NULL DEFAULT 0,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (kind, key)
                )
                """
            )
            # Additive migration — live DBs predate user_rejects. A company/domain
            # row rejected BEFORE this column existed is impossible (the namespaces
            # and the column ship together), so a plain default-0 ADD is safe: no
            # historical data is misread as a user verdict.
            cols = {row[1] for row in conn.execute("PRAGMA table_info(fit_learning)")}
            if "user_rejects" not in cols:
                conn.execute(
                    "ALTER TABLE fit_learning ADD COLUMN user_rejects INTEGER NOT NULL DEFAULT 0"
                )
            conn.commit()
        finally:
            conn.close()

    def record(self, kind: str, key: str, *, kept: bool) -> None:
        """Add one completed run's outcome to the (kind,key) counts.

        No-op on an empty key (an unknown industry / opaque source is never
        learned — silence is not evidence).
        """
        if not kind or not key:
            return
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO fit_learning (kind, key, trials, kept, last_seen)
                    VALUES (?, ?, 1, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(kind, key) DO UPDATE SET
                        trials = trials + 1,
                        kept = kept + excluded.kept,
                        last_seen = CURRENT_TIMESTAMP
                    """,
                    (kind, key, 1 if kept else 0),
                )
                conn.commit()
            finally:
                conn.close()

    def get(self, kind: str, key: str) -> dict[str, int] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT trials, kept, user_rejects FROM fit_learning WHERE kind = ? AND key = ?",
                (kind, key),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {"trials": row[0], "kept": row[1], "user_rejects": row[2]}

    def all(self, kind: str | None = None) -> dict[str, dict[str, int]]:
        conn = self._conn()
        try:
            if kind:
                rows = conn.execute(
                    "SELECT kind, key, trials, kept, user_rejects FROM fit_learning WHERE kind = ?",
                    (kind,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT kind, key, trials, kept, user_rejects FROM fit_learning"
                ).fetchall()
        finally:
            conn.close()
        return {
            f"{r[0]}:{r[1]}": {
                "trials": r[2],
                "kept": r[3],
                "user_rejects": r[4],
            }
            for r in rows
        }

    def should_skip(self, kind: str, key: str) -> bool:
        """True to auto-skip this (kind,key) on the next run.

        Dispatches by namespace: ``company``/``domain`` are skipped on a single
        USER rejection (``user_rejects >= 1``) — one human "not our client"
        verdict outweighs any AI score and needs no trial count, so a purged
        company stays purged. ``industry``/``source`` keep the old contract:
        skipped only once they have enough completed trials AND never once
        became a real lead. No record (never seen) -> keep. Empty key -> keep.
        """
        if not key:
            return False
        row = self.get(kind, key)
        if row is None:
            return False
        if kind in (KIND_COMPANY, KIND_DOMAIN):
            return row["user_rejects"] >= 1
        return row["trials"] >= MIN_TRIALS and row["kept"] == 0

    def reject(self, kind: str, key: str) -> None:
        """Record an explicit USER verdict that this (kind,key) is not a client.

        Unlike :meth:`record`, this is decisive and not gated by trials: a human
        rejection of a real company/domain is treated as ground truth. Repeated
        rejections accumulate (auditable), and existing research counts on the
        row ride along untouched — but the first rejection already wins, because
        :meth:`should_skip` reads ``user_rejects`` and ignores trials/kept for
        the company/domain namespaces. Empty key -> no-op (never learned).
        """
        if not kind or not key:
            return
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO fit_learning (kind, key, trials, kept, user_rejects, last_seen)
                    VALUES (?, ?, 0, 0, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(kind, key) DO UPDATE SET
                        user_rejects = user_rejects + 1,
                        last_seen = CURRENT_TIMESTAMP
                    """,
                    (kind, key),
                )
                conn.commit()
            finally:
                conn.close()

    def reset(self) -> None:
        """Test helper — clear all learning rows."""
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM fit_learning")
                conn.commit()
            finally:
                conn.close()

    # -- convenience wrappers (readability at the call sites) ----------------

    def record_industry(self, industry: str, *, kept: bool) -> None:
        self.record(KIND_INDUSTRY, normalize_industry(industry), kept=kept)

    def should_skip_industry(self, industry: str) -> bool:
        return self.should_skip(KIND_INDUSTRY, normalize_industry(industry))

    def record_source(self, source_url: str, *, kept: bool) -> None:
        self.record(KIND_SOURCE, source_host(source_url), kept=kept)

    def should_skip_source(self, source_url: str) -> bool:
        return self.should_skip(KIND_SOURCE, source_host(source_url))

    def reject_company(self, company_name: str) -> None:
        self.reject(KIND_COMPANY, normalize_company(company_name))

    def should_skip_company(self, company_name: str) -> bool:
        return self.should_skip(KIND_COMPANY, normalize_company(company_name))

    def reject_domain(self, domain: str) -> None:
        self.reject(KIND_DOMAIN, mail_domain(domain))

    def should_skip_domain(self, domain: str) -> bool:
        return self.should_skip(KIND_DOMAIN, mail_domain(domain))
