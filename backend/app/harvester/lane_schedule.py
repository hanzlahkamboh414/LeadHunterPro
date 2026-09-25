"""Harvester lane schedule — the admin screen's control over WHICH lane runs
and for how long.

Why this exists
---------------
The harvester has two lanes with very different cost profiles:

* ``phones`` — mechanical SODA fetches from free license boards. ZERO AI spend.
* ``emails`` — harvest-time AI research (``run_full``). This is where the
  AI budget goes.

Until now the choice between them was frozen in ``.env`` at boot
(``HARVEST_LANE_MODE``). The operator needs to steer it live: "phones only
right now", "emails only", or "cycle automatically — 10 minutes of phones,
then 40 minutes of emails", either repeating all day or for one single cycle.

Shape (deliberately the same as ``app/auth/settings.py``): one SQLite table in
the harvester's OWN database — the harvester domain owns one DB per concern,
so this does not ride in users.db with the auth toggles. It is read once per
harvester pass; because the worker re-reads it at the top of every pass, a
change made in the admin UI takes effect on the NEXT pass
(``HARVESTER_INTERVAL_S``, default 5 min) with no restart and no
re-construction of the worker.

Honest limits (surfaced in the admin UI, never hidden):
* This steers the HARVESTER only. It can never block a user's live Execute
  search — user jobs always win the pipeline budget.
* The phones lane spends no AI at all, so "phones only" means "AI idle in the
  harvester" — but the hourly Source Scout cron runs on its own schedule and
  is NOT controlled here.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from app.core.db_paths import operational_db_path
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary — ONE set of names shared by the store, the worker, the API and
# the admin UI (a second spelling of the same mode is how they drift apart).
# ---------------------------------------------------------------------------

#: ``both``/``phones``/``emails`` pin one behaviour; ``auto`` alternates the
#: two lanes on a clock (see :func:`resolve`).
MODE_BOTH = "both"
MODE_PHONES = "phones"
MODE_EMAILS = "emails"
MODE_AUTO = "auto"
MODES: tuple[str, ...] = (MODE_BOTH, MODE_PHONES, MODE_EMAILS, MODE_AUTO)

#: ``day`` repeats the auto cycle forever; ``once`` runs exactly ONE cycle
#: from the moment the schedule was saved, then falls back to ``both``.
REPEAT_DAY = "day"
REPEAT_ONCE = "once"
REPEATS: tuple[str, ...] = (REPEAT_DAY, REPEAT_ONCE)

#: Slot-length bounds in MINUTES. A 0-minute slot is a zero-length cycle
#: (divide by zero), and a 12-hour slot is not a schedule — it is a pinned
#: mode, which the dedicated modes already express.
MIN_SLOT_MIN = 1.0
MAX_SLOT_MIN = 720.0

DEFAULT_PHONE_MIN = 30.0
DEFAULT_EMAIL_MIN = 90.0


def _parse_iso(ts: str) -> datetime | None:
    """Stored ISO timestamp -> aware datetime (naive treated as UTC)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class LaneSpec:
    """The stored schedule — exactly what the admin screen saves."""

    mode: str = MODE_BOTH
    phone_min: float = DEFAULT_PHONE_MIN
    email_min: float = DEFAULT_EMAIL_MIN
    phone_first: bool = True
    repeat: str = REPEAT_DAY
    #: Anchor for the ``auto`` cycle. Rewritten to "now" on every save, so
    #: saving "10 min phones then 40 min emails" starts that cycle
    #: immediately instead of dropping the operator into an arbitrary slot.
    started_at: str = ""

    def validate(self) -> "LaneSpec":
        """Raise ``ValueError`` on anything the worker could not run.

        The slot bounds are enforced only for ``auto``: in the pinned modes
        the slot numbers are unused, and rejecting a save over an irrelevant
        field would be a confusing error rather than a protective one.
        """
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.repeat not in REPEATS:
            raise ValueError(f"repeat must be one of {REPEATS}, got {self.repeat!r}")
        if self.mode == MODE_AUTO:
            for name, value in (("phone_min", self.phone_min),
                                ("email_min", self.email_min)):
                if not (MIN_SLOT_MIN <= value <= MAX_SLOT_MIN):
                    raise ValueError(
                        f"{name} must be between {MIN_SLOT_MIN:g} and "
                        f"{MAX_SLOT_MIN:g} minutes, got {value!r}"
                    )
        return self


@dataclass(frozen=True)
class LaneDecision:
    """What the worker should do on THIS pass.

    ``mode`` is the EFFECTIVE lane (``both``/``phones``/``emails``) — for an
    ``auto`` spec it is the lane the clock is currently inside, never
    ``auto`` itself. The remaining fields describe the cycle for the admin
    status readout; they are zero/None for the pinned modes.
    """

    mode: str
    spec: LaneSpec
    in_phone_slot: bool | None = None
    cycle_s: float = 0.0
    position_s: float = 0.0
    seconds_to_switch: float | None = None
    one_time_done: bool = False

    @property
    def next_switch_at(self) -> datetime | None:
        """Absolute time of the next lane flip, or None outside ``auto``."""
        if self.seconds_to_switch is None:
            return None
        return datetime.now(timezone.utc) + timedelta(
            seconds=self.seconds_to_switch)


def resolve(spec: LaneSpec, now: datetime) -> LaneDecision:
    """Pure: the schedule + a wall clock -> the lane to run right now.

    ``auto`` anchors its cycle at ``spec.started_at`` (the moment the
    operator saved it), NOT at midnight: "10 minutes of phones, then 40 of
    emails" must start when the button is pressed, otherwise the operator
    lands in the middle of an arbitrary slot.
    """
    if spec.mode != MODE_AUTO:
        return LaneDecision(mode=spec.mode, spec=spec)

    phone_s = spec.phone_min * 60.0
    email_s = spec.email_min * 60.0
    cycle_s = phone_s + email_s
    anchor = _parse_iso(spec.started_at) or now
    elapsed = (now - anchor).total_seconds()
    if elapsed < 0:
        elapsed = 0.0  # clock skew, or a start time saved "in the future"

    if spec.repeat == REPEAT_ONCE and elapsed >= cycle_s:
        # The single cycle is over: both lanes, exactly as if no schedule
        # had been set. The row is deliberately NOT rewritten — the admin
        # screen shows "one-time cycle finished" from this flag instead.
        return LaneDecision(
            mode=MODE_BOTH, spec=spec, cycle_s=cycle_s,
            position_s=cycle_s, one_time_done=True,
        )

    position = elapsed % cycle_s if spec.repeat == REPEAT_DAY else elapsed
    if spec.phone_first:
        in_phone = position < phone_s
        switch_at = phone_s if in_phone else cycle_s
    else:
        in_phone = position >= email_s
        switch_at = cycle_s if in_phone else email_s
    return LaneDecision(
        mode=MODE_PHONES if in_phone else MODE_EMAILS,
        spec=spec,
        in_phone_slot=in_phone,
        cycle_s=cycle_s,
        position_s=position,
        seconds_to_switch=max(0.0, switch_at - position),
    )


class HarvesterLaneStore:
    """One-row SQLite persistence for the lane schedule.

    Lives in ``output/harvester.db`` (the harvester's own DB, gitignored via
    the output/ rule). Every failure degrades to the default spec — a broken
    schedule must never stop the harvester, only leave it on ``both``.
    """

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = operational_db_path(os.path.join(
                os.path.dirname(__file__), "..", "..", "output",
                "harvester.db",
            ))
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._db_path)),
                    exist_ok=True)
        with self._lock:
            conn = self._conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lane_schedule (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    mode TEXT NOT NULL,
                    phone_min REAL NOT NULL,
                    email_min REAL NOT NULL,
                    phone_first INTEGER NOT NULL,
                    repeat TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.commit()
            conn.close()

    def load(self) -> LaneSpec:
        """The stored schedule, or the default (``both``) when unset/broken."""
        try:
            conn = self._conn()
            row = conn.execute(
                "SELECT mode, phone_min, email_min, phone_first, repeat, "
                "started_at FROM lane_schedule WHERE id = 1"
            ).fetchone()
            conn.close()
        except sqlite3.Error as exc:
            logger.warning("LANE SCHEDULE unreadable (%s); using 'both'", exc)
            return LaneSpec()
        if row is None:
            return LaneSpec()
        try:
            return LaneSpec(
                mode=str(row[0]),
                phone_min=float(row[1]),
                email_min=float(row[2]),
                phone_first=bool(row[3]),
                repeat=str(row[4]),
                started_at=str(row[5] or ""),
            ).validate()
        except (ValueError, TypeError) as exc:
            logger.warning("LANE SCHEDULE row invalid (%s); using 'both'", exc)
            return LaneSpec()

    def save(self, spec: LaneSpec, now: datetime) -> LaneSpec:
        """Persist a schedule; the cycle anchor is stamped to ``now``.

        Raises ``ValueError`` (via :meth:`LaneSpec.validate`) for anything the
        worker could not run — the API turns that into an honest 422 rather
        than storing a schedule that would silently do nothing.
        """
        stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        spec = replace(spec, started_at=stamp).validate()
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO lane_schedule "
                "(id, mode, phone_min, email_min, phone_first, repeat, started_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "mode = excluded.mode, phone_min = excluded.phone_min, "
                "email_min = excluded.email_min, "
                "phone_first = excluded.phone_first, repeat = excluded.repeat, "
                "started_at = excluded.started_at",
                (spec.mode, spec.phone_min, spec.email_min,
                 1 if spec.phone_first else 0, spec.repeat, spec.started_at),
            )
            conn.commit()
            conn.close()
        logger.info(
            "HARVEST LANE schedule saved: mode=%s phone=%gmin email=%gmin "
            "first=%s repeat=%s (takes effect on the next pass)",
            spec.mode, spec.phone_min, spec.email_min,
            "phones" if spec.phone_first else "emails", spec.repeat,
        )
        return spec

    def resolve(self, now: datetime) -> LaneDecision:
        """The stored schedule resolved against a wall clock."""
        return resolve(self.load(), now)


_instance: HarvesterLaneStore | None = None


def get_lane_store() -> HarvesterLaneStore:
    """Lazy singleton — resolved through the module attribute so tests can
    monkeypatch ``_instance`` (same indirection as auth settings)."""
    global _instance
    if _instance is None:
        _instance = HarvesterLaneStore()
    return _instance
