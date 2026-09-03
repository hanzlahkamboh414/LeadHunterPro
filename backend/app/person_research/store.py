"""Person Attribution Research — SQLite store (jobs + caches).

SQLite is the primary MVP store (audit correction #3): atomic transactions,
durable, concurrent-safe, no Redis/broker. One file holds the job table, the
append-only evidence, and the domain/query caches.

Evidence is **append-only** per email (audit correction #13): a re-run merges,
never wholesale-replaces, so no prior evidence is lost.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
from typing import Any

from app.person_research.models import (
    AttributionVerdict,
    PersonCandidate,
    ResearchEvidence,
    ResearchStatus,
)


def normalize_email(email: str) -> str:
    """Normalize an address for hashing: trim, lowercase domain, strip mailto.

    ``+tags`` and Gmail-dots are NOT stripped here — that is only safe to do
    for known free-mail providers (which are unresearchable anyway), so those
    decisions belong to triage, not hashing. Hash the normalized form.
    """
    text = (email or "").strip()
    if text.lower().startswith("mailto:"):
        text = text[7:]
    text = text.strip()
    if "@" not in text:
        return ""
    local, _, domain = text.partition("@")
    domain = domain.strip().lower().rstrip(".")
    # IDNA-encode Unicode domains so the same address hashes identically.
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:  # pragma: no cover - invalid domain
        return ""
    return f"{local.strip()}@{domain}"


def email_hash(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


def domain_hash(domain: str) -> str:
    d = (domain or "").strip().lower().rstrip(".")
    return hashlib.sha256(d.encode("utf-8")).hexdigest()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    email_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    domain TEXT NOT NULL,
    research_status TEXT NOT NULL,
    verdict TEXT NOT NULL,
    candidates_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    sources_checked TEXT NOT NULL DEFAULT '[]',
    source_errors TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 1,
    last_researched_at TEXT,
    research_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS domain_cache (
    domain_hash TEXT PRIMARY KEY,
    facts_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    research_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS query_cache (
    query_hash TEXT PRIMARY KEY,
    results_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


class ResearchStore:
    """SQLite-backed research store. One instance per process is fine.

    Thread-safety: sqlite3 with ``check_same_thread=False`` + a lock for the
    local MVP; WAL journal gives crash-safe, concurrent reads.
    """

    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = __import__("threading").Lock()

    # -- jobs ----------------------------------------------------------------

    def create_job(
        self,
        email: str,
        domain: str,
        *,
        research_version: int = 1,
    ) -> str:
        norm = normalize_email(email)
        eh = email_hash(norm)
        with self._lock:
            cur = self._conn.execute(
                "SELECT email_hash FROM jobs WHERE email_hash = ?", (eh,)
            )
            if cur.fetchone() is not None:
                return eh
            self._conn.execute(
                "INSERT OR IGNORE INTO jobs "
                "(email_hash, email, domain, research_status, verdict, "
                " research_version) VALUES (?,?,?,?,?,?)",
                (
                    eh,
                    norm,
                    domain,
                    ResearchStatus.pending.value,
                    AttributionVerdict.unattributed.value,
                    research_version,
                ),
            )
            self._conn.commit()
        return eh

    def pending_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE research_status = ?",
                (ResearchStatus.pending.value,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_job(self, email_hash_: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE email_hash = ?", (email_hash_,)
            ).fetchone()
        return dict(row) if row else None

    def _set_status(self, eh: str, status: ResearchStatus, verdict: AttributionVerdict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET research_status=?, verdict=?, last_researched_at=?"
                " WHERE email_hash=?",
                (status.value, verdict.value, _now(), eh),
            )
            self._conn.commit()

    def mark_researching(self, eh: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET research_status=? WHERE email_hash=?",
                (ResearchStatus.researching.value, eh),
            )
            self._conn.commit()

    def complete_job(self, result: Any, status: ResearchStatus) -> None:
        """Persist a finished ResearchResult (append-only evidence + verdict)."""
        eh = result.email_hash
        with self._lock:
            row = self._conn.execute(
                "SELECT evidence_json FROM jobs WHERE email_hash=?", (eh,)
            ).fetchone()
            existing: list[dict] = json.loads(row["evidence_json"]) if row else []
            # merge: existing evidence preserved, new evidence appended (dedupe).
            merged = _merge_evidence(existing, _evidence_dicts(result))
            self._conn.execute(
                "UPDATE jobs SET research_status=?, verdict=?, candidates_json=?,"
                " evidence_json=?, sources_checked=?, source_errors=?,"
                " attempts=?, last_researched_at=? WHERE email_hash=?",
                (
                    status.value,
                    result.verdict.value,
                    json.dumps([c.to_dict() for c in result.candidates]),
                    json.dumps(merged),
                    json.dumps(result.sources_checked),
                    json.dumps(result.source_errors),
                    result.attempts,
                    _now(),
                    eh,
                ),
            )
            self._conn.commit()

    def recover_interrupted(self) -> None:
        """Any stuck ``researching`` job (crash) returns to ``pending``.

        ``sources_checked`` is preserved, so completed stages are not re-run.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET research_status=? WHERE research_status=?",
                (ResearchStatus.pending.value, ResearchStatus.researching.value),
            )
            self._conn.commit()

    def requeue(self, email_hash_: str, *, force_research: bool = False) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT research_version FROM jobs WHERE email_hash=?", (email_hash_,)
            )
            row = cur.fetchone()
            if row is None:
                return False
            version = row["research_version"] + (1 if force_research else 0)
            self._conn.execute(
                "UPDATE jobs SET research_status=?, research_version=? WHERE email_hash=?",
                (ResearchStatus.pending.value, version, email_hash_),
            )
            self._conn.commit()
        return True

    # -- caches --------------------------------------------------------------

    def get_domain_facts(self, domain: str, research_version: int) -> dict[str, Any] | None:
        dh = domain_hash(domain)
        with self._lock:
            row = self._conn.execute(
                "SELECT facts_json, research_version FROM domain_cache WHERE domain_hash=?",
                (dh,),
            ).fetchone()
        if row is None or row["research_version"] != research_version:
            return None
        return json.loads(row["facts_json"])

    def set_domain_facts(self, domain: str, facts: dict[str, Any], research_version: int) -> None:
        dh = domain_hash(domain)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO domain_cache "
                "(domain_hash, facts_json, fetched_at, research_version) VALUES (?,?,?,?)",
                (dh, json.dumps(facts), _now(), research_version),
            )
            self._conn.commit()

    def get_query_cache(self, query: str) -> list[dict[str, Any]] | None:
        qh = _sha256(query)
        with self._lock:
            row = self._conn.execute(
                "SELECT results_json FROM query_cache WHERE query_hash=?", (qh,)
            ).fetchone()
        return json.loads(row["results_json"]) if row else None

    def set_query_cache(self, query: str, results: list[dict[str, Any]]) -> None:
        qh = _sha256(query)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO query_cache (query_hash, results_json, fetched_at)"
                " VALUES (?,?,?)",
                (qh, json.dumps(results), _now()),
            )
            self._conn.commit()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _evidence_dicts(result: Any) -> list[dict]:
    out: list[dict] = []
    for c in result.candidates:
        for e in c.evidence:
            out.append(e.to_dict())
    return out


def _merge_evidence(existing: list[dict], new: list[dict]) -> list[dict]:
    seen = {(e["evidence_kind"], e.get("canonical_url", e["source_url"]), e.get("snippet", "")) for e in existing}
    merged = list(existing)
    for e in new:
        key = (e["evidence_kind"], e.get("canonical_url", e["source_url"]), e.get("snippet", ""))
        if key not in seen:
            merged.append(e)
            seen.add(key)
    return merged
