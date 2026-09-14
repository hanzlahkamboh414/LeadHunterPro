"""ScoutStore — the source scout's own ``source_scout.db``.

One SQLite file per concern (the harvester.db / phone_leads.db pattern).
The scout's state is NOT lead data: it is the staged-trust lifecycle of
CANDIDATE sources. Leads live in their own vertical stores — this module
never touches them.

Staged trust (no admin anywhere in the loop — founder directive):

    proposed ──mechanical verify──▶ verified ──▶ probation ──▶ promoted
        │                              │            │            │
        └──────────────────────────────┴──── retire ┴────────────┘

    ``proposed``   an AI playbook proposal landed in quarantine — it is
                   never trusted, never dispatched, never injected as
                   proven (§12: a proposal is not evidence)
    ``verified``   the MECHANICAL verifier passed it (real fetch, real
                   shape, real volume, real recency — numbers, not AI
                   opinion)
    ``probation``  agnes judges repeated verified fetches; the pass
                   rate over ``verdicts`` rows is the only promotion
                   currency (verified-only reward — an AI label never
                   promotes anything, same safeguard as dork learning)
    ``promoted``   live: the phones lane may serve it
    ``retired``    dead — with an honest ``retire_reason``. Resurrection
                   is HUMAN-only (the Phase H contract: the loop never
                   re-promotes); ``propose()`` on a retired source_id is
                   a no-op, not a resurrection.

Tables
------
``sources``        one row per candidate source (lifecycle + the
                   SODA-shaped proposal payload as JSON)
``verdicts``       one row per check outcome — mechanical or probation.
                   The single source of truth for the probation score;
                   ``sources.status`` is visibility, never the decision
``known_sources``  the known/dead memory the playbook prompt reads so
                   the AI never re-proposes a proven-dead route (public
                   SearXNG, DDG lite, CSLB's WAF, …)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

_INIT_LOCK = threading.RLock()

# Lifecycle states (staged trust — one direction, retirement from anywhere).
STATUS_PROPOSED = "proposed"
STATUS_VERIFIED = "verified"
STATUS_PROBATION = "probation"
STATUS_PROMOTED = "promoted"
STATUS_RETIRED = "retired"

#: Every non-retired pre-promotion state — the quarantine proper.
QUARANTINE_STATUSES = (STATUS_PROPOSED, STATUS_VERIFIED, STATUS_PROBATION)

#: The state machine's legal forward edges. Illegal transitions raise —
#: a proposal can never skip a stage (e.g. proposed → promoted), so the
#: §12 rule ("never injected as proven") is enforced by the store, not
#: by every caller remembering it. The stamp column records ARRIVAL;
#: probation needs no own stamp (it shares verified_at's row history —
#: status itself says where it is).
_TRANSITIONS: dict[tuple[str, str], str | None] = {
    (STATUS_PROPOSED, STATUS_VERIFIED): "verified_at",
    (STATUS_VERIFIED, STATUS_PROBATION): None,
    (STATUS_PROBATION, STATUS_PROMOTED): "promoted_at",
}

#: Which verdict stages exist. ``mechanical`` rows are the verifier's
#: numbers; ``probation`` rows are agnes's per-fetch judgements.
STAGE_MECHANICAL = "mechanical"
STAGE_PROBATION = "probation"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def default_db_path() -> str:
    """The real output DB path (``backend/output/source_scout.db``).

    Exposed so the phones lane can EXISTENCE-CHECK the file before ever
    constructing a store — a machine that never ran the scout must not get
    a DB created just because the harvester imported the module.
    """
    return os.path.join(
        os.path.dirname(__file__), "..", "..", "output", "source_scout.db",
    )


class ScoutStore:
    """SQLite persistence for the staged-trust source lifecycle."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = default_db_path()
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
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'proposed',
                    provenance TEXT NOT NULL DEFAULT '',
                    proposed_at TEXT NOT NULL,
                    verified_at TEXT NOT NULL DEFAULT '',
                    promoted_at TEXT NOT NULL DEFAULT '',
                    retired_at TEXT NOT NULL DEFAULT '',
                    retire_reason TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS verdicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_verdicts_source
                    ON verdicts (source_id, stage)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS known_sources (
                    source_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()

    # -- proposals (quarantine entry) ---------------------------------------

    def propose(
        self,
        source_id: str,
        kind: str,
        name: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        provenance: str = "",
    ) -> bool:
        """Add a new candidate to quarantine.

        Returns False when the source_id already exists — including a
        RETIRED one: re-proposing a proven-dead route is a no-op, never
        a resurrection (the Phase H human-only-resurrection contract).
        """
        conn = self._conn()
        try:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO sources
                    (source_id, kind, name, endpoint, payload, status,
                     provenance, proposed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source_id.strip().lower(), kind.strip().lower(),
                 name.strip()[:200], endpoint.strip()[:500],
                 json.dumps(payload or {}, sort_keys=True),
                 STATUS_PROPOSED, provenance[:200], _now()),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def get(self, source_id: str) -> dict[str, Any] | None:
        """One source row with ``payload`` parsed back to a dict."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM sources WHERE source_id = ?",
                (source_id.strip().lower(),),
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in conn.execute(
                "SELECT * FROM sources LIMIT 0").description]
            out = dict(zip(cols, row, strict=True))
            try:
                out["payload"] = json.loads(out.get("payload") or "{}")
            except (TypeError, ValueError):
                out["payload"] = {}
            return out
        finally:
            conn.close()

    def list_status(self, status: str,
                    limit: int = 100) -> list[dict[str, Any]]:
        """All sources in one lifecycle state (oldest proposal first)."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT source_id, kind, name, endpoint, status,
                       provenance, proposed_at
                FROM sources WHERE status = ?
                ORDER BY proposed_at ASC LIMIT ?
                """,
                (status, limit),
            ).fetchall()
            cols = ("source_id", "kind", "name", "endpoint", "status",
                    "provenance", "proposed_at")
            return [dict(zip(cols, r, strict=True)) for r in rows]
        finally:
            conn.close()

    def list_quarantine(self, limit: int = 100) -> list[dict[str, Any]]:
        """Everything still in the staged-trust pipeline (not promoted,
        not retired) — the quarantine proper."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT source_id, kind, name, endpoint, status,
                       provenance, proposed_at
                FROM sources WHERE status IN (?, ?, ?)
                ORDER BY proposed_at ASC LIMIT ?
                """,
                (*QUARANTINE_STATUSES, limit),
            ).fetchall()
            cols = ("source_id", "kind", "name", "endpoint", "status",
                    "provenance", "proposed_at")
            return [dict(zip(cols, r, strict=True)) for r in rows]
        finally:
            conn.close()

    def promoted_payloads(self, limit: int = 500) -> dict[str, dict[str, Any]]:
        """``{source_id: payload}`` for every PROMOTED source.

        The phones lane's serving map (soda.py reads this on every
        coverage lookup): a promoted source is the only scout state the
        lead lanes are ever allowed to serve — everything else is
        quarantine by construction.
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT source_id, payload FROM sources "
                "WHERE status = ? LIMIT ?",
                (STATUS_PROMOTED, limit),
            ).fetchall()
            out: dict[str, dict[str, Any]] = {}
            for source_id, payload in rows:
                try:
                    out[source_id] = json.loads(payload or "{}")
                except (TypeError, ValueError):
                    out[source_id] = {}
            return out
        finally:
            conn.close()

    # -- lifecycle transitions -------------------------------------------------

    def _transition(self, source_id: str, to_status: str) -> dict[str, Any]:
        """Advance one legal forward edge, stamping the arrival time.

        Raises ValueError on an unknown source or an illegal edge, so a
        proposal can never skip a stage by accident or design.
        """
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT status FROM sources WHERE source_id = ?",
                (source_id.strip().lower(),),
            ).fetchone()
            if not row:
                raise ValueError(f"unknown source_id: {source_id!r}")
            current = row[0]
            stamp_col = _TRANSITIONS.get((current, to_status))
            if (current, to_status) not in _TRANSITIONS:
                raise ValueError(
                    f"illegal transition {current!r} -> {to_status!r} "
                    f"for {source_id!r}"
                )
            if stamp_col:
                conn.execute(
                    f"UPDATE sources SET status = ?, {stamp_col} = ? "
                    "WHERE source_id = ?",
                    (to_status, _now(), source_id.strip().lower()),
                )
            else:
                conn.execute(
                    "UPDATE sources SET status = ? WHERE source_id = ?",
                    (to_status, source_id.strip().lower()),
                )
            conn.commit()
        finally:
            conn.close()
        out = self.get(source_id)
        assert out is not None  # the row existed a moment ago
        return out

    def mark_verified(self, source_id: str) -> dict[str, Any]:
        """proposed → verified (the mechanical verifier passed)."""
        return self._transition(source_id, STATUS_VERIFIED)

    def start_probation(self, source_id: str) -> dict[str, Any]:
        """verified → probation (agnes takes over repeated checks)."""
        return self._transition(source_id, STATUS_PROBATION)

    def promote(self, source_id: str) -> dict[str, Any]:
        """probation → promoted (only after the service checked the
        probation score — the store keeps the edge single and legal)."""
        return self._transition(source_id, STATUS_PROMOTED)

    def retire(self, source_id: str, reason: str) -> None:
        """Any live state → retired, with an honest reason.

        Retirement is legal from every non-retired state: a proposal can
        fail verification, a probation can stall, and a PROMOTED source
        can go bad in production (circuit-breaker auto-retire).
        """
        conn = self._conn()
        try:
            conn.execute(
                """
                UPDATE sources
                SET status = ?, retired_at = ?, retire_reason = ?
                WHERE source_id = ? AND status != ?
                """,
                (STATUS_RETIRED, _now(), reason[:500],
                 source_id.strip().lower(), STATUS_RETIRED),
            )
            conn.commit()
        finally:
            conn.close()

    # -- verdicts (the only promotion currency) ---------------------------------

    def record_verdict(self, source_id: str, stage: str, verdict: bool,
                       detail: str = "") -> None:
        """Append one check outcome (mechanical or probation).

        Verdicts are the single source of truth: promotion reads them,
        status never overrides them, and an unknown source_id is a caller
        bug we refuse to swallow.
        """
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM sources WHERE source_id = ?",
                (source_id.strip().lower(),),
            ).fetchone()
            if not row:
                raise ValueError(f"unknown source_id: {source_id!r}")
            conn.execute(
                """
                INSERT INTO verdicts
                    (source_id, stage, verdict, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_id.strip().lower(), stage,
                 "pass" if verdict else "fail", detail[:500], _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def probation_score(self, source_id: str) -> tuple[int, int, float]:
        """(passes, total, rate) over PROBATION-stage verdicts.

        Mechanical verdicts are deliberately excluded — probation is
        judged on agnes's repeated verified fetches, never on the
        verifier's own numbers (verified-only reward).
        """
        conn = self._conn()
        try:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN verdict = 'pass' THEN 1 ELSE 0 END),
                    COUNT(*)
                FROM verdicts WHERE source_id = ? AND stage = ?
                """,
                (source_id.strip().lower(), STAGE_PROBATION),
            ).fetchone()
            passes, total = int(row[0] or 0), int(row[1] or 0)
            rate = (passes / total) if total else 0.0
            return passes, total, rate
        finally:
            conn.close()

    def recent_verdicts(self, source_id: str, stage: str = "",
                        limit: int = 20) -> list[bool]:
        """Latest-first verdict outcomes (True = pass) for one source,
        optionally scoped to one stage.

        The circuit breaker's streak math reads this: the newest verdict
        first, so a leading run of False values is the current failure
        streak.
        """
        conn = self._conn()
        try:
            if stage:
                rows = conn.execute(
                    """
                    SELECT verdict FROM verdicts
                    WHERE source_id = ? AND stage = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (source_id.strip().lower(), stage, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT verdict FROM verdicts
                    WHERE source_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (source_id.strip().lower(), limit),
                ).fetchall()
            return [r[0] == "pass" for r in rows]
        finally:
            conn.close()

    # -- known/dead memory (playbook prompt input) -------------------------------

    def upsert_known(self, source_id: str, status: str,
                     reason: str = "") -> None:
        """Record a known-good or known-dead route (upsert).

        Seeded from the lead-source verification sweeps and the dead
        routes we already paid for (public SearXNG 429s, DDG lite,
        CSLB's F5 WAF). The playbook prompt reads this so the AI never
        re-proposes a proven-dead route.
        """
        if status not in ("good", "dead"):
            raise ValueError(f"known status must be 'good' or 'dead', "
                             f"got {status!r}")
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO known_sources (source_id, status, reason,
                                           updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (source_id) DO UPDATE SET
                    status = excluded.status,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (source_id.strip().lower(), status, reason[:500], _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def known_map(self) -> dict[str, dict[str, str]]:
        """{source_id: {status, reason}} — the whole known/dead memory."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT source_id, status, reason FROM known_sources"
            ).fetchall()
            return {
                r[0]: {"status": r[1], "reason": r[2]} for r in rows
            }
        finally:
            conn.close()


_store: ScoutStore | None = None
_store_lock = threading.Lock()


def get_store() -> ScoutStore:
    """Process-wide singleton bound to the real output DB. Tests
    monkeypatch this — construction is lazy so importing the module never
    creates the file."""
    global _store
    with _store_lock:
        if _store is None:
            _store = ScoutStore()
        return _store
