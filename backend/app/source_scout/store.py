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

#: V2 coverage-engine states (docs/architecture/coverage_engine_v2.md §7).
#: The V1 proposal states stay legal until Phase 4 deletes the proposal
#: stage — V2 rows move through these instead:
#:     untried ─▶ probing ─▶ adapter_draft ─▶ probation ─▶ promoted
#:                    │              ▲               │    │         │
#:                    ▼              └─ schema_mismatch  │    exhausted(30d re-arm)
#:               blocked (403/429 → next path)           └─▶ dead (404/410 only)
#: ``dead`` is the ONLY permanent state; ``blocked``/``exhausted`` re-arm
#: via ``re_arm()`` after ``next_retry_at`` passes.
STATUS_UNTRIED = "untried"
STATUS_PROBING = "probing"
STATUS_ADAPTER_DRAFT = "adapter_draft"
STATUS_EXHAUSTED = "exhausted"
STATUS_BLOCKED = "blocked"
STATUS_DEAD = "dead"

#: The prober's 5 access paths in priority order (coverage_engine_v2.md
#: §4) — the vocabulary ``record_probe_success`` validates against.
#: boards.py's ACCESS_PATH_HINTS is THIS set plus the legacy
#: soda/socrata hints — one vocabulary, never two.
PROBER_PATHS = ("bulk_file", "open_data_api", "xhr_json", "html_form", "pdf")

#: V2 legal forward edges (V1 edges live in _TRANSITIONS). Every edge out
#: of blocked/exhausted is the RE-ARM path; promoted→probing is format rot
#: (adapter rewrite, never silent retirement); probation→probing and
#: adapter_draft→probing are the validator/schema_mismatch bounces.
_V2_TRANSITIONS: dict[tuple[str, str], str | None] = {
    (STATUS_UNTRIED, STATUS_PROBING): None,
    (STATUS_UNTRIED, STATUS_BLOCKED): "gate_fail_reason",
    (STATUS_UNTRIED, STATUS_DEAD): "gate_fail_reason",
    (STATUS_PROBING, STATUS_ADAPTER_DRAFT): None,
    (STATUS_PROBING, STATUS_BLOCKED): "gate_fail_reason",
    (STATUS_PROBING, STATUS_DEAD): "gate_fail_reason",
    (STATUS_ADAPTER_DRAFT, STATUS_PROBATION): None,
    (STATUS_ADAPTER_DRAFT, STATUS_PROBING): None,
    (STATUS_ADAPTER_DRAFT, STATUS_BLOCKED): "gate_fail_reason",
    (STATUS_ADAPTER_DRAFT, STATUS_DEAD): "gate_fail_reason",
    (STATUS_PROBATION, STATUS_PROMOTED): "promoted_at",
    (STATUS_PROBATION, STATUS_PROBING): "gate_fail_reason",
    (STATUS_PROBATION, STATUS_EXHAUSTED): "gate_fail_reason",
    (STATUS_PROMOTED, STATUS_PROBING): "gate_fail_reason",
    (STATUS_PROMOTED, STATUS_EXHAUSTED): "gate_fail_reason",
    (STATUS_PROMOTED, STATUS_DEAD): "gate_fail_reason",
    (STATUS_BLOCKED, STATUS_PROBING): None,
    (STATUS_EXHAUSTED, STATUS_PROBING): None,
}

#: The 8 V2 statuses + the legacy proposal set — the full legal vocabulary
#: any row may hold while the Phase-4 swap is pending.
ALL_STATUSES = frozenset((
    STATUS_PROPOSED, STATUS_VERIFIED, STATUS_PROBATION, STATUS_PROMOTED,
    STATUS_RETIRED, STATUS_UNTRIED, STATUS_PROBING, STATUS_ADAPTER_DRAFT,
    STATUS_EXHAUSTED, STATUS_BLOCKED, STATUS_DEAD,
))

#: Registry metadata columns added by the Phase-1 migration (v2). Old DBs
#: get ALTER TABLE ADD COLUMN; fresh DBs create them in the base CREATE.
_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("seed_domain", "TEXT NOT NULL DEFAULT ''"),     # phones | emails | both
    ("state", "TEXT NOT NULL DEFAULT ''"),           # authoritative jurisdiction
    ("base_url", "TEXT NOT NULL DEFAULT ''"),
    ("access_path", "TEXT NOT NULL DEFAULT ''"),     # bulk_file|open_data_api|xhr_json|html_form|pdf
    ("fetch_spec", "TEXT NOT NULL DEFAULT '{}'"),
    ("field_map", "TEXT NOT NULL DEFAULT '{}'"),
    ("trade_mapping", "TEXT NOT NULL DEFAULT '{}'"),
    ("capabilities", "TEXT NOT NULL DEFAULT '{}'"),  # {phone: {present, fill_rate}, ...}
    ("estimated_rows", "INTEGER NOT NULL DEFAULT 0"),
    ("rows_consumed", "INTEGER NOT NULL DEFAULT 0"),
    ("next_retry_at", "TEXT NOT NULL DEFAULT ''"),
    ("last_fetched_at", "TEXT NOT NULL DEFAULT ''"),
    ("last_verified_at", "TEXT NOT NULL DEFAULT ''"),
    ("gate_fail_reason", "TEXT NOT NULL DEFAULT ''"),
)

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
            self._migrate_v2(conn)
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

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        """Phase-1 registry migration — additive, idempotent, non-destructive.

        Adds the V2 columns to an existing V1 DB (fresh DBs baked them
        into the CREATE). Existing rows keep their status — nothing is
        rewritten, nothing is retired.
        """
        existing = {
            r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()
        }
        for col, decl in _V2_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE sources ADD COLUMN {col} {decl}")

    # -- V2 registry (seeds + status model) ------------------------------------

    def seed_upsert(self, source_id: str, *, seed_domain: str, state: str,
                    kind: str = "seed", name: str = "", endpoint: str = "",
                    base_url: str = "", seed_meta: dict[str, Any] | None = None
                    ) -> None:
        """Create or refresh a seed row WITHOUT touching its lifecycle.

        Metadata-only upsert: on conflict, every V2 column and payload
        seed block refreshes, but ``status``/timestamps survive — re-seeding
        an exhausted source must not resurrect it.
        """
        seed_meta = dict(seed_meta or {})
        existing = self.get(source_id)
        status = existing.get("status", STATUS_UNTRIED) if existing else STATUS_UNTRIED
        proposed_at = existing.get("proposed_at") if existing else _now()
        payload = dict(existing.get("payload", {}) if existing else {})
        payload["seed"] = seed_meta
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO sources (source_id, kind, name, endpoint,
                    payload, status, provenance, proposed_at,
                    seed_domain, state, base_url)
                VALUES (?, ?, ?, ?, ?, ?, 'seed', ?, ?, ?, ?)
                ON CONFLICT (source_id) DO UPDATE SET
                    name = excluded.name,
                    endpoint = excluded.endpoint,
                    payload = excluded.payload,
                    seed_domain = excluded.seed_domain,
                    state = excluded.state,
                    base_url = excluded.base_url
                """,
                (source_id.strip().lower(), kind, name[:200],
                 endpoint[:500], json.dumps(payload, sort_keys=True),
                 status, proposed_at,
                 seed_domain.strip().lower(), state.strip().upper()[:2],
                 base_url.strip()[:500]),
            )
            conn.commit()
        finally:
            conn.close()

    def _v2_transition(self, source_id: str, to_status: str, *,
                       reason: str = "", next_retry_at: str = "") -> dict[str, Any]:
        """Advance one V2 legal edge, then return the fresh row.

        ``reason`` lands in gate_fail_reason (the honest, specific why —
        dead only on 404/410; blocked carries 403/429/captcha + retry path).
        V2 edges are checked FIRST so shadowed V1 names can't double-fire.
        """
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT status FROM sources WHERE source_id = ?",
                (source_id.strip().lower(),))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"unknown source_id: {source_id!r}")
            current = row[0]
            edge = (current, to_status)
            if edge in _V2_TRANSITIONS:
                stamp_col = _V2_TRANSITIONS[edge]
                sql = "UPDATE sources SET status = ?, gate_fail_reason = ?"
                args: list[Any] = [to_status, reason[:500]]
                if stamp_col and stamp_col != "gate_fail_reason":
                    # a real arrival stamp (promoted_at); gate_fail_reason
                    # IS the reason column — never overwrite it with a time
                    sql += f", {stamp_col} = ?"
                    args.append(_now())
                if to_status in (STATUS_BLOCKED, STATUS_EXHAUSTED) and stamp_col != "promoted_at":
                    sql += ", next_retry_at = ?"
                    args.append(next_retry_at)
                elif to_status == STATUS_PROBING:
                    # re-arm: a fresh probe starts with a clean clock
                    sql += ", next_retry_at = ?"
                    args.append("")
                sql += " WHERE source_id = ?"
                args.append(source_id.strip().lower())
                conn.execute(sql, args)
                conn.commit()
            else:
                # legacy V1 edge (proposal pipeline still lives until Phase 4)
                return self._transition(source_id, to_status)
            conn.close()
        except Exception:
            conn.close()
            raise
        out = self.get(source_id)
        assert out is not None
        return out

    def start_probing(self, source_id: str) -> dict[str, Any]:
        """untried → probing (the access prober starts walking paths)."""
        return self._v2_transition(source_id, STATUS_PROBING)

    def mark_adapter_draft(self, source_id: str,
                           reason: str = "") -> dict[str, Any]:
        """probing → adapter_draft (prober found a path; AI writes the
        adapter next — but store-level, the edge is all we enforce here)."""
        return self._v2_transition(source_id, STATUS_ADAPTER_DRAFT, reason=reason)

    def enter_probation(self, source_id: str) -> dict[str, Any]:
        """adapter_draft → probation (validator passed; N-row dry run)."""
        return self._v2_transition(source_id, STATUS_PROBATION)

    def mark_blocked(self, source_id: str, reason: str,
                     next_retry_at: str = "") -> dict[str, Any]:
        """Any V2 pre-promotion state → blocked (403/429/captcha).

        Blocked is NEVER dead: the prober re-tries the next access path
        (or after next_retry_at when every path is exhausted).
        """
        return self._v2_transition(
            source_id, STATUS_BLOCKED, reason=reason, next_retry_at=next_retry_at)

    def mark_dead(self, source_id: str, reason: str) -> dict[str, Any]:
        """→ dead — 404/410 ONLY, permanent (the one terminal state)."""
        return self._v2_transition(source_id, STATUS_DEAD, reason=reason)

    def mark_exhausted(self, source_id: str, reason: str,
                       next_retry_at: str) -> dict[str, Any]:
        """→ exhausted — dup>90% × 3; re-arms 30 days out."""
        return self._v2_transition(
            source_id, STATUS_EXHAUSTED, reason=reason, next_retry_at=next_retry_at)

    def re_arm(self, source_id: str) -> dict[str, Any]:
        """blocked/exhausted → probing once next_retry_at has passed.

        The caller checks the clock; the store owns the legality of the
        edge (an early re-arm bounces here).
        """
        row = self.get(source_id)
        assert row is not None
        if row["status"] not in (STATUS_BLOCKED, STATUS_EXHAUSTED):
            raise ValueError(
                f"re_arm only from blocked/exhausted, got {row['status']!r}")
        return self._v2_transition(source_id, STATUS_PROBING)

    def rework_adapter(self, source_id: str, reason: str) -> dict[str, Any]:
        """→ probing — the schema_mismatch/format-rot bounce (§7).

        Legal from adapter_draft (validator gate fail), probation
        (validator re-check) and promoted (fetch error after promotion):
        the source returns to probing for a fresh adapter + re-validation,
        never silent retirement.
        """
        return self._v2_transition(source_id, STATUS_PROBING, reason=reason)

    def _set(self, source_id: str, **cols: str | int) -> None:
        """One-row UPDATE of plain columns (feeder helper; column names
        are literal call-site kwargs only)."""
        assignments = ", ".join(f"{k} = ?" for k in cols)
        conn = self._conn()
        try:
            conn.execute(
                f"UPDATE sources SET {assignments} WHERE source_id = ?",
                (*cols.values(), source_id.strip().lower()),
            )
            conn.commit()
        finally:
            conn.close()

    def record_probe_success(self, source_id: str,
                             access_path: str) -> dict[str, Any]:
        """The prober found a working path: record it, stamp the fetch,
        clear any stale failure reason (gate_fail_reason reflects the
        CURRENT state, never history)."""
        if access_path not in PROBER_PATHS:
            raise ValueError(
                f"unknown access_path {access_path!r} "
                f"(not one of {PROBER_PATHS})")
        self._set(source_id, access_path=access_path,
                  last_fetched_at=_now(), gate_fail_reason="")
        out = self.get(source_id)
        assert out is not None
        return out

    def record_gate_reason(self, source_id: str, reason: str) -> dict[str, Any]:
        """Record an honest reason WITHOUT a status change — e.g. a seed
        row that cannot be probed yet (no base_url): it stays in place,
        visible in the queue, never silently skipped."""
        self._set(source_id, gate_fail_reason=reason[:500])
        out = self.get(source_id)
        assert out is not None
        return out

    def update_validation(self, source_id: str, *,
                          capabilities: dict[str, Any] | None = None,
                          estimated_rows: int | None = None,
                          gate_fail_reason: str | None = None
                          ) -> dict[str, Any]:
        """Persist the validator's measurements (the numbers the quota
        formula and export priority read), stamped as last verified.

        ``capabilities`` arrives with measured fill_rate per capability —
        the registry stores {present, fill_rate} at validation time (§3).
        """
        cols: dict[str, str | int] = {"last_verified_at": _now()}
        if capabilities is not None:
            cols["capabilities"] = json.dumps(capabilities, sort_keys=True)
        if estimated_rows is not None:
            cols["estimated_rows"] = int(estimated_rows)
        if gate_fail_reason is not None:
            cols["gate_fail_reason"] = gate_fail_reason[:500]
        self._set(source_id, **cols)
        out = self.get(source_id)
        assert out is not None
        return out

    def record_adapter(self, source_id: str, *, fetch_spec: dict[str, Any],
                       field_map: dict[str, str],
                       trade_mapping: dict[str, str],
                       capabilities: dict[str, Any]) -> dict[str, Any]:
        """Persist the AI-written adapter contract (§5).

        Capabilities arrive as present-flags ONLY — the fill_rate numbers
        the quota formula reads are the VALIDATOR's measurement, never the
        AI's claim, which is why this method cannot accept them.
        """
        self._set(source_id,
                  fetch_spec=json.dumps(fetch_spec, sort_keys=True),
                  field_map=json.dumps(field_map, sort_keys=True),
                  trade_mapping=json.dumps(trade_mapping, sort_keys=True),
                  capabilities=json.dumps(capabilities, sort_keys=True))
        out = self.get(source_id)
        assert out is not None
        return out

    def promoted_for_vertical(self, vertical: str) -> list[dict[str, Any]]:
        """PROMOTED sources where capabilities[vertical].present is true.

        The router gate (Phase-1 exit criterion): phone demand must never
        bind to a source whose capabilities.phone.present is false — the
        TX-mechanical class of waste is prevented at the SELECT, not at
        fetch time.
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT source_id, capabilities, access_path, state, "
                "estimated_rows FROM sources WHERE status = ?",
                (STATUS_PROMOTED,)).fetchall()
            out: list[dict[str, Any]] = []
            for source_id, caps, access_path, state, est in rows:
                try:
                    caps = json.loads(caps or "{}")
                except (TypeError, ValueError):
                    caps = {}
                slot = caps.get(vertical, {})
                if isinstance(slot, dict) and slot.get("present"):
                    out.append({
                        "source_id": source_id,
                        "capabilities": caps,
                        "access_path": access_path,
                        "state": state,
                        "estimated_rows": est,
                    })
            return out
        finally:
            conn.close()

    def phone_queue(self, limit: int = 100) -> list[dict[str, Any]]:
        """Seed rows still workable for the phones lane, ranked.

        Terminal (dead) and dormant (exhausted until re-arm, blocked with
        a future next_retry_at) rows drop out; trade_scope='none' rows
        (state has no licensing board) never enter the queue at all.
        Order: priority_rank asc (CBP establishment count — the real
        quantity lever), the determinism the prober needs.
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT source_id, payload, state, status, next_retry_at "
                "FROM sources WHERE seed_domain IN ('phones', 'both')").fetchall()
            now = _now()
            workable: list[dict[str, Any]] = []
            for source_id, payload, state, status, next_retry_at in rows:
                try:
                    seed = (json.loads(payload or "{}").get("seed") or {})
                except (TypeError, ValueError):
                    seed = {}
                if seed.get("trade_scope") == "none":
                    continue
                if status == STATUS_DEAD:
                    continue
                if status == STATUS_BLOCKED and next_retry_at > now:
                    continue
                workable.append({
                    "source_id": source_id, "state": state,
                    "priority_rank": int(seed.get("priority_rank", 0)),
                    "trade_scope": seed.get("trade_scope", ""),
                    "status": status,
                })
            workable.sort(key=lambda r: (r["priority_rank"], r["source_id"]))
            return workable[:limit]
        finally:
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
