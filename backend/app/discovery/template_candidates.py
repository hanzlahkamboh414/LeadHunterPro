"""Layer-1 generated-template lifecycle store (Phase H).

Phase F/G closed the DROP side of the self-learning loop: dead plan-holder
dorks are auto-skipped once their yield says so. Phase H closes the ADD side —
an LLM occasionally proposes NEW dork phrasings (maintenance script), and this
store holds their LIFECYCLE so the discovery source can dispatch the survivors
as first-class templates. The yield-LEARNING stays exactly where Phase G put it
(``discovery_template_yield``, :class:`~app.discovery.yield_learning.
DiscoveryYieldStore`); this table says only *what was proposed* and *what state
it is in*. The yield table is the single source of truth for dispatch.

The dispatch rule (what actually runs in the plan-holder source):
  * a candidate DISPATCHES unless proven dead — same contract as any dork:
    NOT (``trials >= MIN_TRIALS`` AND ``working == 0``). Default-keep is the
    safe choice: one quiet week must not kill a new angle.
  * a candidate whose yield reaches ``PROVEN_GOOD`` (3) WORKING leads is
    lazy-promoted ``active`` — it earned its place through the loop, never by
    being proposed (§12: a generated dork is never injected as proven).
  * a candidate proven dead is lazy-demoted ``dropped`` and stops dispatching.
    Resurrection is HUMAN-only (P-G): clearing the yield record via
    :meth:`~app.discovery.yield_learning.DiscoveryYieldStore.delete_template`
    is the one way a dropped angle comes back — the loop never re-promotes.

``status`` is lifecycle VISIBILITY (the report + the source), never the
decision: ``effective_dorks()`` reads the yield every time so counts decay and
promote naturally on the same record research writes.

Only Layer-1 ``dork`` candidates exist in Phase H; the ``layer`` column admits
Layer-2 deep-query candidates later without a schema change.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)

#: WORKING leads a dork must produce before it is treated as a proven-good
#: (auto-promoted) template. Layer-1 metric — a working lead is a dossier that
#: shows in the leads list (contact_now/nurture). Layer-2 uses ``verified``
#: evidence instead; that metric belongs to the deep-query follow-on.
PROVEN_GOOD = 3

_DEFAULT_DB = os.path.join(
    os.path.dirname(__file__), "..", "..", "output", "lead_research.db"
)

#: Serializes writes across concurrent discovery passes / research threads.
_write_lock = threading.Lock()


class TemplateCandidateStore:
    """Persistent lifecycle of LLM-generated (or human) candidate templates.

    One small table in the lead-research DB (``template_candidates``). Each row
    is a dork template PATTERN (with ``{industry}``/``{location}`` placeholders
    — the same key space as ``discovery_template_yield``) with a lifecycle
    status. Additive only: no existing table is touched, and deleting/resetting
    this table returns dispatch to exactly the pre-Phase-H static dorks.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS template_candidates (
            label TEXT PRIMARY KEY,
            layer TEXT NOT NULL DEFAULT 'dork',
            status TEXT NOT NULL DEFAULT 'candidate',
            source TEXT NOT NULL DEFAULT 'llm',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        # A fresh connection per call (shared across threads — reusing one
        # SQLite connection across threads is not safe). Same pattern as the
        # yield stores.
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = self._conn()
        try:
            conn.execute(self._SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # -- lifecycle writes ------------------------------------------------

    def propose(self, label: str, layer: str = "dork") -> bool:
        """Stage a generated candidate; False when the label already exists.

        ``label`` is the PATTERN (placeholders intact). The PK is the
        dedupe: the same phrasing is never staged twice, whatever its status.
        ``layer`` separates the discovery lanes: ``'dork'`` = Layer-1 plan-
        holder PDF dorks, ``'web'`` = Layer-2 web-search angles (Inc 2) —
        the earn-or-die yield loop is shared, the lane is not.
        """
        label = (label or "").strip()
        if not label:
            return False
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO template_candidates "
                    "(label, layer, status, source) VALUES (?, ?, 'candidate', 'llm')",
                    (label, layer),
                )
                conn.commit()
                return conn.total_changes > 0
            finally:
                conn.close()

    def _set_status(self, label: str, status: str) -> None:
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE template_candidates SET status = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE label = ?",
                    (status, label),
                )
                conn.commit()
            finally:
                conn.close()

    # -- dispatch rule -----------------------------------------------------

    def effective_dorks(
        self, yield_store: object | None = None, segment: str = "",
        layer: str = "dork",
    ) -> list[str]:
        """Active patterns the source should dispatch, in order.

        Single source of truth is the yield store (:class:`DiscoveryYieldStore`
        over the SAME db file). A candidate dispatches unless proven dead; when
        proven-good it is lazy-promoted, when dead lazy-demoted. ``yield_store``
        may be any object exposing ``get(label)`` and ``should_skip(label)`` —
        the real store or a test double.

        ``segment`` (Phase I) is the normalized ``segment_key`` of the running
        query. The dispatch gate and the promote/demote evidence both read the
        SEGMENT row — so a candidate dead for ``roofing | dallas tx`` drops
        there while still dispatching where it earns working leads (global-drop
        still wins).

        ``layer`` selects the discovery lane: ``'dork'`` (Layer-1 plan-holder
        PDFs) or ``'web'`` (Layer-2 web-search angles, Inc 2).
        """
        with _write_lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT label FROM template_candidates WHERE layer = ?",
                    (layer,),
                ).fetchall()
            finally:
                conn.close()
        labels = [r[0] for r in rows]
        out: list[str] = []
        for label in labels:
            status = self._status(label)
            # Dispatch rule = NOT proven-dead — the exact should_skip the source
            # already applies to the static dorks. A candidate earns its place
            # (or dies) on the SAME yield record.
            if yield_store and yield_store.should_skip(label, segment):
                if status != "dropped":
                    self._set_status(label, "dropped")
                continue
            # Proven-good -> active: earned through the loop, never by proposal.
            row = yield_store.get(label, segment) if yield_store else None
            working = row["working"] if row else 0
            if working >= PROVEN_GOOD and status != "active":
                self._set_status(label, "active")
            out.append(label)
        return out

    def _status(self, label: str) -> str:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT status FROM template_candidates WHERE label = ?", (label,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else ""

    # -- reporting / tests ------------------------------------------------

    def all(self) -> list[dict[str, str]]:
        """Every candidate row, for the script report and tests."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT label, layer, status, source FROM template_candidates "
                "ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()
        return [
            {"label": r[0], "layer": r[1], "status": r[2], "source": r[3]}
            for r in rows
        ]

    def reset(self) -> None:
        """Test helper — clear all candidates."""
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM template_candidates")
                conn.commit()
            finally:
                conn.close()