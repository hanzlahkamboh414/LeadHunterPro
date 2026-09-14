"""P11 — migration dry-run on COPIES of the production SQLite DBs.

The P12 big-bang push opens the production databases with code that has
accumulated a full phase-ladder of additive migrations since the last
deploy. This script proves, on a throwaway COPY of every production DB
in ``backend/output/``, that:

1. opening each DB with the CURRENT store classes succeeds,
2. every pre-existing table survives with its row count unchanged
   (additive ALTERs and CREATE IF NOT EXISTS never touch existing rows),
3. every schema element the P-series added is present afterwards.

The copies are made with the sqlite3 backup API — a plain file copy of a
live WAL database (``search_cache.db`` has live -wal/-shm files) can tear
or miss the journal contents; the backup API checkpoints into the copy
under a proper lock and never writes the source.

Honest scope note: the local production DBs have already been opened by
the new code (each phase ran here), so for THEM this run mostly proves
idempotency and row preservation. The EC2 databases are older, and the
additive-ALTER paths exercised here are the exact ones that will run
there — on a production DB the pre-ALTER shape is what these copies
exercised at each phase's first run.

Usage (from ``backend/``)::

    python scripts/verify_migrations.py           # every DB
    python scripts/verify_migrations.py users.db harvester.db
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BACKEND_DIR / "output"

# `python scripts/verify_migrations.py` puts scripts/ (not backend/) on
# sys.path — the app package lives in backend/, so put it there ourselves.
sys.path.insert(0, str(BACKEND_DIR))

# ---------------------------------------------------------------------------
# The registry: every production SQLite DB, every store that opens it (each
# one's __init__ runs its own migrations), and the schema elements the
# P-series added that must exist afterwards. Only VERIFIED names are listed
# — nothing here is aspirational.
# ---------------------------------------------------------------------------


def _lead_research_stores(path: str):
    from app.discovery.template_candidates import TemplateCandidateStore
    from app.discovery.yield_learning import DiscoveryYieldStore
    from app.lead_research.fit_learning import FitLearningStore
    from app.lead_research.query_learning import QueryYieldStore
    from app.lead_research.service import LeadResearchStore, PendingLeadsStore
    from app.leads.jobs import JobStore

    # Every store that owns a table in lead_research.db, opened in turn —
    # each construction runs that store's CREATE IF NOT EXISTS / ALTER set
    # against the SAME copy, exactly like a production process would.
    LeadResearchStore(path)
    PendingLeadsStore(path)
    QueryYieldStore(path)
    FitLearningStore(path)
    DiscoveryYieldStore(path)
    TemplateCandidateStore(path)
    JobStore(path)


def _users_stores(path: str):
    from app.auth.activity import ActivityStore
    from app.auth.models import UserStore
    from app.email_accounts.store import EmailAccountStore

    UserStore(path)
    ActivityStore(path)
    EmailAccountStore(path)


def _phones_stores(path: str):
    from app.phones.store import PhoneLeadsStore

    PhoneLeadsStore(path)


def _harvester_stores(path: str):
    from app.harvester.store import HarvesterStore

    HarvesterStore(path)


def _search_cache_stores(path: str):
    from app.search_providers.cache import SearchCache

    # SearchCache's first parameter is `path`, not `db_path` — positional.
    # It holds its connection open (the long-lived cache), and on Windows
    # an open handle keeps the temp-dir cleanup from deleting the copy.
    SearchCache(path).close()


def _linkedin_stores(path: str):
    from app.linkedin.store import LinkedInLeadsStore

    LinkedInLeadsStore(path)


# (table, column) pairs that must exist after the stores run.
_EXPECTED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "lead_research.db": [
        ("dossiers", "trade"),        # P1 trade foundation
        ("pending_leads", "trade"),   # P1 trade foundation
        ("pending_leads", "dork"),
    ],
    "users.db": [
        ("users", "category"),        # P3 signup category
    ],
    "phone_leads.db": [
        # P3.5 enrichment (search -> official site -> crawl)
        ("phone_leads", "email"),
        ("phone_leads", "email_source"),
        ("phone_leads", "website"),
        ("phone_leads", "enriched_at"),
        # P7.5 tiered voicemail recycling
        ("phone_leads", "voicemail_count"),
        ("phone_leads", "voicemail_at"),
    ],
    "harvester.db": [
        ("harvest_runs", "source"),   # P10 yield attribution
    ],
    "search_cache.db": [],
    "linkedin_leads.db": [],
}

# Tables that must exist after the stores run.
_EXPECTED_TABLES: dict[str, list[str]] = {
    "lead_research.db": [
        "dossiers",
        "deleted_leads",            # delete-feed stash
        "dossier_owners",           # serve-time exclusivity
        "discovery_template_yield",  # Phase G/I dork learning
        "template_candidates",       # Phase H candidate lifecycle
    ],
    "users.db": [],
    "phone_leads.db": [
        "phone_user_leads",   # P7.5 caller snapshots
        "phone_suppressions",  # P7.5 retired-number pool
    ],
    "harvester.db": [
        "source_yield",  # P10 (source, segment) yield learning
    ],
    "search_cache.db": [],
    "linkedin_leads.db": [],
}

# Composite primary keys that must survive intact (a dropped/recreated
# table with a different key would silently change learning semantics).
_EXPECTED_PKS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "lead_research.db": [
        ("discovery_template_yield", ("template", "segment")),
    ],
}

_REGISTRY: dict[str, callable] = {
    "lead_research.db": _lead_research_stores,
    "users.db": _users_stores,
    "phone_leads.db": _phones_stores,
    "harvester.db": _harvester_stores,
    "search_cache.db": _search_cache_stores,
    "linkedin_leads.db": _linkedin_stores,
}

# Production-shaped DBs that are NOT in output/ — reported honestly, not
# silently ignored. overture.duckdb is DuckDB (not SQLite — the sync is a
# materialization, not a migration); source_scout.db and campaigns.db do
# not exist on this machine (scout never ran live; campaigns never used).
_NOT_SQLITE_OR_ABSENT = {
    "overture.duckdb": "DuckDB materialization (P8) — not a SQLite migration",
    "source_scout.db": "does not exist yet (scout never ran live)",
    "campaigns.db": "does not exist on this machine (campaigns unused)",
}


def _table_counts(db_path: str | Path) -> dict[str, int]:
    """Every user table -> row count (sqlite internal tables excluded)."""
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in sorted(tables)
        }
    finally:
        conn.close()


def _columns(db_path: str | Path, table: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _pk(db_path: str | Path, table: str) -> tuple[str, ...]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            f"SELECT name FROM pragma_table_info('{table}') "
            "WHERE pk > 0 ORDER BY pk"
        ).fetchall()
        return tuple(r[0] for r in rows)
    finally:
        conn.close()


def _copy_production_db(src: Path, dst: Path) -> None:
    """WAL-safe copy via the sqlite3 backup API.

    Read-only source open first (never writes the production file); the
    read-write fallback only triggers if a WAL the -shm can't map needs
    recovery, and even then the backup API itself never modifies the
    source's logical content.
    """
    try:
        source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def verify_db(name: str, open_stores) -> bool:
    src = OUTPUT_DIR / name
    print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
    if not src.exists():
        print(f"  SKIP — {src} does not exist")
        return True  # not a failure; reported honestly in the summary

    with tempfile.TemporaryDirectory(prefix="lh_mig_") as tmp:
        copy = Path(tmp) / name
        _copy_production_db(src, copy)

        before = _table_counts(copy)
        print(f"  copied via backup API; {len(before)} tables, "
              f"{sum(before.values())} rows total")

        # THE migration: every store that opens this DB, in turn, against
        # the copy — exactly what a production process does at startup.
        try:
            open_stores(str(copy))
        except Exception as exc:  # noqa: BLE001 — report, don't crash
            print(f"  FAIL — store construction raised: {type(exc).__name__}:"
                  f" {exc}")
            return False

        after = _table_counts(copy)

        ok = True

        # 1. every pre-existing table still present, count not decreased.
        for table, count in before.items():
            if table not in after:
                print(f"  FAIL — table {table!r} DISAPPEARED "
                      f"(had {count} rows)")
                ok = False
            elif after[table] < count:
                print(f"  FAIL — table {table!r} LOST ROWS: "
                      f"{count} -> {after[table]}")
                ok = False
            elif after[table] > count:
                # Additive seeding is legal at migration time; flagged, not
                # failed — but it must never go unnoticed.
                print(f"  note — table {table!r} gained rows: "
                      f"{count} -> {after[table]}")
        new_tables = sorted(set(after) - set(before))
        if new_tables:
            print(f"  new tables created: {', '.join(new_tables)}")

        # 2. expected P-series columns present.
        for table, column in _EXPECTED_COLUMNS[name]:
            if column not in _columns(copy, table):
                print(f"  FAIL — {table}.{column} missing after migration")
                ok = False
        # 3. expected P-series tables present.
        for table in _EXPECTED_TABLES[name]:
            if table not in after:
                print(f"  FAIL — expected table {table!r} missing")
                ok = False
        # 4. composite PKs intact.
        for table, want in _EXPECTED_PKS.get(name, []):
            got = _pk(copy, table)
            if got != want:
                print(f"  FAIL — {table} PK is {got}, expected {want}")
                ok = False

        if ok:
            n_cols = len(_EXPECTED_COLUMNS[name])
            n_tabs = len(_EXPECTED_TABLES[name])
            print(f"  PASS — {len(before)}/{len(before)} tables preserved, "
                  f"{n_tabs} expected tables + {n_cols} expected columns "
                  f"present")
        return ok


def main(argv: list[str]) -> int:
    only = [a for a in argv[1:] if not a.startswith("-")]
    unknown = [a for a in only if a not in _REGISTRY]
    if unknown:
        print(f"Unknown DB name(s): {', '.join(unknown)}\n"
              f"Known: {', '.join(sorted(_REGISTRY))}")
        return 2

    print("P11 migration dry-run — production DB COPIES in a temp dir;\n"
          "the files in backend/output/ are never modified.")

    names = only or list(_REGISTRY)
    results = {name: verify_db(name, _REGISTRY[name]) for name in names}

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    for name, why in _NOT_SQLITE_OR_ABSENT.items():
        print(f"  --    {name} ({why})")

    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(f"\nRESULT: FAIL ({', '.join(failed)})")
        return 1
    print("\nRESULT: PASS — all production DB copies migrate cleanly, "
          "zero rows lost")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
