"""Bounce learning — real send/reply outcomes as email ground truth.

P5-Lite part 2. RCPT probing is blocked from every network we control
(AWS Trust & Safety rejection, IPv6 throttle, consumer ISP), so the
platform's own Gmail outreach (Phase E2) becomes the verification signal:
a REPLY proves the mailbox exists; a mailer-daemon DSN proves it does
not. Every outcome is recorded here and fed back into the heuristic
verifier (:mod:`app.email.heuristic_verifier`) — the same
learn-from-real-outcomes loop the dork and fit stores already follow.

Honesty rules (CLAUDE.md §1/§12):
  * Only REAL evidence is ever recorded — an outcome row exists iff a
    message genuinely came back (reply or DSN). Nothing is inferred,
    guessed, or seeded.
  * ``bounced`` and ``delivered`` are facts about a moment; the latest
    fact wins (a mailbox that answered once and later bounced is, today,
    dead). The evidence column keeps the provenance.
  * The store NEVER marks anything by itself — callers (the campaign
    scheduler) decide what a Gmail event means; this module only
    remembers.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading

#: What an outcome row can say. Both are real evidence, never a guess.
OUTCOMES = ("bounced", "delivered")


def _email_hash(email: str) -> str:
    """Same deterministic hash as the lead-research store (sha256[:16])."""
    return hashlib.sha256(email.lower().strip().encode()).hexdigest()[:16]


class BounceStore:
    """SQLite store of per-email send outcomes. One row per email.

    Default DB lives in ``backend/output/email_outcomes.db`` (the same
    output dir every other store uses). Thread-safe via a lock around the
    shared connection — the scheduler runs in its own thread.
    """

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "output",
                "email_outcomes.db",
            )
        self._db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_outcomes (
                email_hash TEXT PRIMARY KEY,
                email      TEXT NOT NULL,
                domain     TEXT NOT NULL,
                outcome    TEXT NOT NULL CHECK (outcome IN
                            ('bounced', 'delivered')),
                evidence   TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT
                            (datetime('now'))
            )
            """
        )
        self._conn.commit()

    # -- write ---------------------------------------------------------------

    def record(self, email: str, outcome: str, evidence: str = "") -> bool:
        """Record one real outcome for *email*; False on a bad outcome word.

        Upserts: the latest fact replaces the earlier one, keeping the
        evidence of the newest event. A record is only ever written by a
        caller that genuinely observed the event.
        """
        if outcome not in OUTCOMES:
            return False
        addr = (email or "").strip().lower()
        if "@" not in addr:
            return False
        domain = addr.rsplit("@", 1)[1]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO email_outcomes
                    (email_hash, email, domain, outcome, evidence)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(email_hash) DO UPDATE SET
                    outcome = excluded.outcome,
                    evidence = excluded.evidence,
                    created_at = excluded.created_at
                """,
                (_email_hash(addr), addr, domain, outcome,
                 (evidence or "")[:300]),
            )
            self._conn.commit()
        return True

    # -- read ----------------------------------------------------------------

    def lookup(self, email: str) -> str | None:
        """``"bounced"`` / ``"delivered"`` for a known email, else None."""
        addr = (email or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT outcome FROM email_outcomes WHERE email_hash = ?",
                (_email_hash(addr),),
            ).fetchone()
        return row["outcome"] if row else None

    def counts(self) -> dict[str, int]:
        """Total rows per outcome (for logs/admin reports)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT outcome, COUNT(*) AS n FROM email_outcomes "
                "GROUP BY outcome"
            ).fetchall()
        return {r["outcome"]: r["n"] for r in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
