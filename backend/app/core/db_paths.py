"""Fail-closed selection of the verified operational SQLite database."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3


def operational_db_path(legacy_path: str) -> str:
    """Use the existing path unless an already-built unified DB is enabled.

    This only selects a database. It never creates or migrates one; callers
    with an explicit test/custom path need not use this helper.
    """
    configured = os.environ.get("LEADHUNTER_UNIFIED_DB_PATH", "").strip()
    if not configured:
        return legacy_path
    target = Path(configured)
    if not target.is_absolute():
        raise RuntimeError("unified database path must be absolute")
    if not target.is_file():
        raise RuntimeError("unified database does not exist")
    try:
        with sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True) as conn:
            marked = conn.execute(
                "SELECT version FROM unified_metadata"
            ).fetchall() == [(1,)]
            healthy = conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
    except sqlite3.Error as exc:
        raise RuntimeError("not a verified unified database") from exc
    if not marked or not healthy:
        raise RuntimeError("not a verified unified database")
    return str(target)
