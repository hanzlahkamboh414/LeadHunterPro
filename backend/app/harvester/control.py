"""Durable on/off/schedule switch shared by the API and harvester service."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing

CONTROL_MODES = ("off", "on", "schedule")


class HarvesterControlStore:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or os.path.join(
            os.path.dirname(__file__), "..", "..", "output", "harvester.db"
        )
        os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS harvester_control ("
                "id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL, "
                "changed_at REAL NOT NULL, heartbeat_at REAL NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO harvester_control(id,mode,changed_at) "
                "VALUES(1,'off',?)", (time.time(),)
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def read(self) -> tuple[str, float]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT mode,heartbeat_at FROM harvester_control WHERE id=1"
            ).fetchone()
        return (str(row[0]), float(row[1])) if row else ("off", 0.0)

    def mode(self) -> str:
        return self.read()[0]

    def set_mode(self, mode: str) -> None:
        if mode not in CONTROL_MODES:
            raise ValueError("mode must be off, on, or schedule")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE harvester_control SET mode=?,changed_at=? WHERE id=1",
                (mode, time.time()),
            )

    def heartbeat(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE harvester_control SET heartbeat_at=? WHERE id=1",
                (time.time(),),
            )


_instance: HarvesterControlStore | None = None


def get_control_store() -> HarvesterControlStore:
    global _instance
    if _instance is None:
        _instance = HarvesterControlStore()
    return _instance
