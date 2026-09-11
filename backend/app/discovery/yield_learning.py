"""Deterministic Layer-1 (discovery) yield learning loop (Phase G + Phase I).

The research layer (:mod:`app.lead_research.query_learning`) already learns,
per SEARCH TEMPLATE, whether the URLs it returned became cited/verified
evidence, and drops templates that never do. Phase G extends the SAME loop to
the discovery layer (Layer 1): each plan-holder DORK template is a "template"
whose returned PDFs become companies, and the question is whether those
companies ever become a WORKING lead (a dossier that actually shows in the
leads list).

The old hand-measured dork numbers ("the 5-of-8 dorks that worked") were a
one-time probe baked into the source. This store makes that judgement live and
continuous, without any extra AI call or provider credit:

  trials   — times the dork template's search was actually dispatched
  working  — of the companies produced under that dork, how many became a
             WORKING lead (visible dossier — dead-domain / non-construction /
             sub-threshold are not working)

After ``MIN_TRIALS`` dispatched runs with zero working leads, the dork is
auto-dropped at dispatch time so no future pass spends a provider credit on it.
Until a dork has enough data it is KEPT (default-keep is the safe choice:
dropping on one bad run would starve discovery of a dork that merely had a
quiet week).

Phase I — SEGMENT-AWARE yield. The discovery query is always a (trade,
location) pair, and a dork that works for one segment can be dead for another.
Rows are therefore keyed by ``(template, segment)`` where ``segment`` is the
normalized ``segment_key(trade, location)``; ``segment=''`` is the GLOBAL row
(what Phase G recorded). :meth:`should_skip` applies the SAME segment hierarchy
as the research layer (Phase F): a global drop is authoritative; a segment
decides for itself only once it has its OWN ``MIN_TRIALS``; otherwise it falls
back to the global keep. Segmentation is what lets the loop *target coverage*:
a dork proven dead for ``roofing | dallas tx`` stops spending credits there
while still dispatching where it earns working leads.

CREDIT-SAFE CONTRACTS (mirrors the research layer):
  * attribution is IMMUTABLE at issue time — a company is credited to the
    dork that surfaced its PDF, never rewritten later;
  * a dork is only credited ``working`` when a company it produced becomes a
    genuinely visible dossier (the research layer's verdict, not a count of
    "found something");
  * companies with no dork attribution (e.g. served from a cache a later run,
    or from a seam with no per-dork signal) credit NOTHING — silence is not
    evidence;
  * resurrection is HUMAN-only (:meth:`delete_template`), never auto-promoted.

Only freshly discovered dork-produced companies feed the loop; the store is
shared with the pipeline that commits outcomes at research time, on the SAME
db file (so discovery dispatch and research credit read one consistent record).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading

logger = logging.getLogger(__name__)

#: Dispatched runs before a zero-working dork is considered proven useless.
MIN_TRIALS = 12

_DEFAULT_DB = os.path.join(
    os.path.dirname(__file__), "..", "..", "output", "lead_research.db"
)

#: Serializes writes across concurrent research threads / discovery passes.
_write_lock = threading.Lock()

#: Collapse any run of whitespace (incl. newlines) to one space.
_WS = re.compile(r"\s+")

#: Separator between the trade and location halves of a segment key.
_SEGMENT_SEP = "|"


def segment_key(trade: str, location: str) -> str:
    """Stable, case/whitespace-insensitive key for a (trade, location) query.

    The SAME key must be derivable from the discovery source (which knows
    ``industry``/``location``) and from the pipeline research credit (which
    knows ``trade``/``location``) — e.g. ``Roofing`` + ``"Dallas TX"`` and
    ``roofing`` + ``"Dallas   tx "`` both become ``roofing | dallas tx``. The
    trade/location halves are dropped when blank so a bare query still yields
    a deterministic key.
    """
    parts = []
    for text in (trade, location):
        cleaned = _WS.sub(" ", (text or "").strip().lower()).strip()
        if cleaned:
            parts.append(cleaned)
    return f" {_SEGMENT_SEP} ".join(parts) if parts else ""


class DiscoveryYieldStore:
    """Persistent record of per-(dork-template, segment) discovery yield.

    One small table in the lead-research DB (``discovery_template_yield``).
    Each row is a dork template label + segment with:
      trials   — times the dork's search was actually dispatched
      working  — of the companies produced under it, how many became a
                 WORKING lead (visible dossier)
    ``segment=''`` is the GLOBAL row (pre-Phase-I single-key behavior).
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        # A fresh connection per call: the store is shared across threads and
        # reusing one connection across threads is not safe in SQLite. Writes
        # are serialized under the module lock; reads are cheap.
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS discovery_template_yield (
            template TEXT NOT NULL,
            segment TEXT NOT NULL DEFAULT '',
            trials INTEGER NOT NULL DEFAULT 0,
            working INTEGER NOT NULL DEFAULT 0,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (template, segment)
        )
        """

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = self._conn()
        try:
            conn.execute(self._SCHEMA)
            # Phase I migration — live DBs predate the segment column and have a
            # single-column (template) PK. Additive rebuild (same as Phase F):
            # every legacy row becomes the GLOBAL row (segment=''), and the PK
            # widens to (template, segment). No historical count is lost or
            # misread.
            pk = [r[1] for r in conn.execute("PRAGMA table_info(discovery_template_yield)") if r[5] > 0]
            if pk != ["template", "segment"]:
                conn.execute(
                    "ALTER TABLE discovery_template_yield RENAME TO _discovery_template_yield_old"
                )
                conn.execute(self._SCHEMA)
                conn.execute(
                    "INSERT INTO discovery_template_yield "
                    "(template, segment, trials, working, last_seen) "
                    "SELECT template, '', trials, working, last_seen "
                    "FROM _discovery_template_yield_old"
                )
                conn.execute("DROP TABLE _discovery_template_yield_old")
            conn.commit()
        finally:
            conn.close()

    def record_dispatch(self, template: str, segment: str = "") -> None:
        """Add one dispatched dork search to the (template, segment) trial count.

        Called by the plan-holder source the moment a dork's search is actually
        issued — the credit is recorded at issue time, never backfilled for a
        dork that was skipped. ``segment`` is the normalized ``segment_key`` of
        the running query (global when ``''``).
        """
        if not template:
            return
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO discovery_template_yield (template, segment, trials, last_seen)
                    VALUES (?, ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(template, segment) DO UPDATE SET
                        trials = trials + 1,
                        last_seen = CURRENT_TIMESTAMP
                    """,
                    (template, segment),
                )
                conn.commit()
            finally:
                conn.close()

    def record_working(self, template: str, segment: str = "") -> None:
        """Credit one company produced under ``template``+``segment`` as WORKING.

        Called at research time when a dork-attributed company becomes a
        visible dossier. Upserts so a cross-run credit (a company discovered
        earlier, researched now) still lands even when no dispatch row exists
        yet in this db — a working lead is never lost for want of a trial row.
        """
        if not template:
            return
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO discovery_template_yield (template, segment, trials, working, last_seen)
                    VALUES (?, ?, 0, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(template, segment) DO UPDATE SET
                        working = working + 1,
                        last_seen = CURRENT_TIMESTAMP
                    """,
                    (template, segment),
                )
                conn.commit()
            finally:
                conn.close()

    def get(self, template: str, segment: str = "") -> dict[str, int] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT trials, working FROM discovery_template_yield "
                "WHERE template = ? AND segment = ?",
                (template, segment),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {"trials": row[0], "working": row[1]}

    def all(self) -> dict[str, dict[str, int]]:
        """Every row keyed ``template`` (global) or ``template{sep}segment``."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT template, segment, trials, working FROM discovery_template_yield"
            ).fetchall()
        finally:
            conn.close()
        out: dict[str, dict[str, int]] = {}
        for tpl, seg, trials, working in rows:
            key = tpl if not seg else f"{tpl}{_SEGMENT_SEP}{seg}"
            out[key] = {"trials": trials, "working": working}
        return out

    def should_skip(self, template: str, segment: str = "") -> bool:
        """True to drop this dork template+segment on the next dispatch.

        Segment hierarchy (Phase I, mirror of Phase F):
          * a GLOBAL drop is authoritative — never overridden, one-way (a dead
            template never costs another 12 trials per bucket to confirm);
          * a segment decides for itself only once it has its OWN ``MIN_TRIALS``;
          * otherwise it falls back to the global decision (KEEP default).
        Dropped only when the deciding row has enough real dispatches AND never
        once produced a working lead. No record -> keep.
        """
        if not template:
            return False
        global_row = self.get(template, "")
        if (global_row is not None
                and global_row["trials"] >= MIN_TRIALS
                and global_row["working"] == 0):
            return True  # global DROP — authoritative
        if segment:
            seg_row = self.get(template, segment)
            if seg_row is not None and seg_row["trials"] >= MIN_TRIALS:
                return seg_row["working"] == 0
        return False  # global KEEP default

    def delete_template(self, template: str, segment: str = "") -> None:
        """Manual, logged override — resurrect a dork template (+segment).

        Resurrection is a HUMAN decision only (never auto-promote). Removes the
        row so the one-way drop is cleared until the user decides otherwise.
        """
        if not template:
            return
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "DELETE FROM discovery_template_yield WHERE template = ? AND segment = ?",
                    (template, segment),
                )
                conn.commit()
            finally:
                conn.close()

    def reset(self) -> None:
        """Test helper — clear all yield rows."""
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM discovery_template_yield")
                conn.commit()
            finally:
                conn.close()