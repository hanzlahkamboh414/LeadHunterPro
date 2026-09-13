"""HarvesterStore — the background harvester's own ``harvester.db``.

One SQLite file per concern (the campaigns.db / phone_leads.db pattern).
The harvester's state is NOT lead data: it is demand signals, daily quota
counters, harvest history (rotation + backoff), and the staleness
re-verify queue. Leads live in their own vertical stores — this module
never touches them.

Tables
------
``demand``        trade×state search pressure (every user shortfall or
                  search bumps the weight; the harvester harvests what
                  users actually ask for, most-demanded first)
``quota_log``     per-day stocked-row counters per vertical (the big-bang
                  quotas: 2,000 emails / 5,000 phones per day)
``harvest_runs``  one row per lane pass (outcome + stocked count) — the
                  rotation memory ("this pair was harvested 3h ago") and
                  the honest telemetry trail
``reverify_queue``trade×state pairs whose pool stock has gone stale; the
                  harvester drains this queue when it is otherwise idle
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

_INIT_LOCK = threading.RLock()

#: The 50 states + DC — the demand log stores 2-letter codes, and the
#: emails lane extracts them from free-text locations ("Dallas TX",
#: "Harris County, Texas"). Backend-side map (the frontend's
#: data/locations.ts is not importable here).
US_STATE_ABBRS: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}

_STATE_ABBRS = frozenset(US_STATE_ABBRS.values())

#: 2-letter code -> full state name — the emails lane's run_full gets the
#: same "Texas"-style location vocabulary a user search would use.
US_STATE_NAMES: dict[str, str] = {
    abbr: name.title() for name, abbr in US_STATE_ABBRS.items()
}


def state_from_location(location: str) -> str:
    """The 2-letter state code from a free-text location, or ''.

    Handles the trailing-code form ("Dallas TX", "Vancouver, WA") and the
    full-name form ("Texas", "Harris County, Texas"). Anything unparsable
    is an honest '' — a guessed state would silently mis-rank demand.
    """
    s = (location or "").strip()
    if not s:
        return ""
    tail = s.rsplit(",", 1)[-1].strip()
    if len(tail) == 2 and tail.upper() in _STATE_ABBRS:
        return tail.upper()
    if len(s) >= 2 and s[-2:].upper() in _STATE_ABBRS and not s[-3].isalpha():
        return s[-2:].upper()
    if tail.lower() in US_STATE_ABBRS:
        return US_STATE_ABBRS[tail.lower()]
    return ""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class HarvesterStore:
    """SQLite persistence for demand, quotas, run history, re-verify."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "output",
                "harvester.db",
            )
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
                CREATE TABLE IF NOT EXISTS demand (
                    trade TEXT NOT NULL,
                    state TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE (trade, state)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS quota_log (
                    day TEXT NOT NULL,
                    vertical TEXT NOT NULL,
                    stocked INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (day, vertical)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS harvest_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vertical TEXT NOT NULL,
                    trade TEXT NOT NULL,
                    state TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    stocked INTEGER NOT NULL DEFAULT 0,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reverify_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    enqueued_at TEXT NOT NULL,
                    processed_at TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.commit()
            conn.close()

    # -- demand ----------------------------------------------------------------

    def record_demand(self, trade: str, state: str, weight: float = 1.0) -> None:
        """Bump the search pressure for one trade×state pair (upsert)."""
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO demand (trade, state, weight, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (trade, state) DO UPDATE SET
                    weight = weight + excluded.weight,
                    updated_at = excluded.updated_at
                """,
                (trade.strip().lower(), (state or "").strip().upper()[:2],
                 float(weight), _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def top_demand(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most-demanded pairs first (weight DESC, oldest signal ASC)."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT trade, state, weight FROM demand
                ORDER BY weight DESC, updated_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {"trade": r[0], "state": r[1], "weight": r[2]} for r in rows
            ]
        finally:
            conn.close()

    # -- quotas ------------------------------------------------------------------

    def quota_used(self, vertical: str, day: str | None = None) -> int:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT stocked FROM quota_log WHERE day = ? AND vertical = ?",
                (day or _today(), vertical),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def quota_room(self, vertical: str, daily_limit: int,
                   day: str | None = None) -> int:
        """How many more rows this vertical may stock today (never < 0)."""
        return max(0, daily_limit - self.quota_used(vertical, day))

    def record_stocked(self, vertical: str, n: int,
                       day: str | None = None) -> None:
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO quota_log (day, vertical, stocked)
                VALUES (?, ?, ?)
                ON CONFLICT (day, vertical) DO UPDATE SET
                    stocked = stocked + excluded.stocked
                """,
                (day or _today(), vertical, int(n)),
            )
            conn.commit()
        finally:
            conn.close()

    # -- run history (rotation / backoff) ------------------------------------------

    def record_run(self, vertical: str, trade: str, state: str,
                   outcome: str, stocked: int = 0, detail: str = "") -> None:
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO harvest_runs
                    (vertical, trade, state, outcome, stocked, detail,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (vertical, trade.strip().lower(),
                 (state or "").strip().upper()[:2], outcome, int(stocked),
                 detail[:500], _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def last_run_at(self, vertical: str, trade: str, state: str) -> str:
        """The latest harvest time for one pair ('' = never harvested)."""
        conn = self._conn()
        try:
            row = conn.execute(
                """
                SELECT created_at FROM harvest_runs
                WHERE vertical = ? AND trade = ? AND state = ?
                ORDER BY id DESC LIMIT 1
                """,
                (vertical, trade.strip().lower(),
                 (state or "").strip().upper()[:2]),
            ).fetchone()
            return row[0] if row else ""
        finally:
            conn.close()

    def recent_runs(self, vertical: str, limit: int = 25) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT vertical, trade, state, outcome, stocked, detail,
                       created_at
                FROM harvest_runs WHERE vertical = ?
                ORDER BY id DESC LIMIT ?
                """,
                (vertical, limit),
            ).fetchall()
            cols = ("vertical", "trade", "state", "outcome", "stocked",
                    "detail", "created_at")
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    # -- staleness re-verify queue ---------------------------------------------------

    def enqueue_reverify(self, trade: str, state: str, reason: str) -> None:
        """Queue a stale trade×state pair for a freshness re-harvest
        (idempotent per unprocessed pair — one row per pending item)."""
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO reverify_queue (trade, state, reason, enqueued_at)
                SELECT ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM reverify_queue
                    WHERE trade = ? AND state = ? AND processed_at = ''
                )
                """,
                (trade.strip().lower(), (state or "").strip().upper()[:2],
                 reason[:300], _now(),
                 trade.strip().lower(), (state or "").strip().upper()[:2]),
            )
            conn.commit()
        finally:
            conn.close()

    def take_reverify(self, limit: int = 1) -> list[dict[str, Any]]:
        """Pop the oldest unprocessed re-verify items (marked processed)."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT id, trade, state, reason FROM reverify_queue
                WHERE processed_at = '' ORDER BY id ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            items = [
                {"id": r[0], "trade": r[1], "state": r[2], "reason": r[3]}
                for r in rows
            ]
            for item in items:
                conn.execute(
                    "UPDATE reverify_queue SET processed_at = ? WHERE id = ?",
                    (_now(), item["id"]),
                )
            conn.commit()
            return items
        finally:
            conn.close()

    def reverify_pending(self) -> int:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM reverify_queue WHERE processed_at = ''"
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()


_store: HarvesterStore | None = None
_store_lock = threading.Lock()


def get_store() -> HarvesterStore:
    """Process-wide singleton bound to the real output DB. Tests
    monkeypatch this — construction is lazy so importing the module never
    creates the file."""
    global _store
    with _store_lock:
        if _store is None:
            _store = HarvesterStore()
        return _store


def record_demand_safe(trade: str, state: str, weight: float = 1.0) -> None:
    """The API-seam helper: record demand, never break the request.

    The demand signal is telemetry — a harvester DB hiccup must never
    fail a user's search (the same guard rule as the LinkedIn byproduct).
    """
    try:
        get_store().record_demand(trade, state, weight)
    except Exception:  # noqa: BLE001 — telemetry only, never fatal
        import logging
        logging.getLogger(__name__).exception(
            "harvester demand recording failed for %s/%s — request continues",
            trade, state,
        )
