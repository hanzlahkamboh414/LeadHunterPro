"""Build a verified, offline SQLite consolidation without changing live files.

This prepares a *copy* only. The application is not redirected here: that
requires a separate, tested persistence cutover. Existing phone workflow rows
are tagged to the first tenant in the copy; raw board inventory remains shared.
Pass explicit source paths so backup, scratch, and empty databases can never
be included by a broad glob.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

FIRST_TENANT_ID = "the-best-estimators-llc"
FIRST_TENANT_NAME = "The Best Estimators LLC"
_RESERVED_TABLES = {
    "tenants", "tenant_memberships", "unified_metadata", "email_oauth_pending",
}
_PRIVATE_PHONE_TABLES = {
    "phone_lead_owners", "phone_user_leads", "phone_claim_events",
    "phone_daily_limits", "phone_call_events", "phone_wrong_archive",
}
_SAVED_COLUMNS = {
    "id", "user_id", "phone", "person_name", "business_name", "trade",
    "city", "state", "source", "source_url", "license_status", "email",
    "email_source", "website", "kind", "note", "created_at", "updated_at",
    "tenant_id",
}
_LIMIT_COLUMNS = {"user_id", "daily_limit", "tenant_id"}
_EMAIL_ACCOUNT_COLUMNS = {
    "id", "user_id", "provider", "email", "display_name", "access_token",
    "refresh_token", "token_expires_at", "status", "scopes", "created_at",
    "updated_at", "tenant_id",
}
_CAMPAIGN_COLUMNS = {
    "id", "user_id", "account_id", "name", "subject", "body", "status",
    "paused_reason", "resume_at", "start_at", "daily_limit", "delay_min_s",
    "delay_max_s", "created_at", "updated_at", "ai_personalize", "tenant_id",
}
_WORKFLOW_SCHEMAS = {
    "phone_lead_owners": (
        {"lead_id", "user_id", "created_at", "batch_at", "tenant_id"},
        "lead_id INTEGER NOT NULL, user_id TEXT NOT NULL, "
        "created_at TEXT NOT NULL, batch_at TEXT NOT NULL DEFAULT '', "
        "tenant_id TEXT NOT NULL REFERENCES tenants(id), "
        "UNIQUE (tenant_id, lead_id, user_id)",
        None,
    ),
    "phone_claim_events": (
        {"id", "lead_id", "user_id", "phone", "created_at", "tenant_id"},
        "id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, "
        "user_id TEXT NOT NULL, phone TEXT NOT NULL, created_at TEXT NOT NULL, "
        "tenant_id TEXT NOT NULL REFERENCES tenants(id)",
        "idx_phone_claim_day",
    ),
    "phone_call_events": (
        {"id", "user_id", "lead_id", "phone", "person_name", "business_name", "trade", "state", "action", "created_at", "tenant_id"},
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, "
        "lead_id INTEGER NOT NULL, phone TEXT NOT NULL, "
        "person_name TEXT NOT NULL DEFAULT '', business_name TEXT NOT NULL DEFAULT '', "
        "trade TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT '', "
        "action TEXT NOT NULL, created_at TEXT NOT NULL, "
        "tenant_id TEXT NOT NULL REFERENCES tenants(id)",
        "idx_phone_call_day",
    ),
    "phone_wrong_archive": (
        {"id", "user_id", "phone", "snapshot_json", "created_at", "recovered_at", "tenant_id"},
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, "
        "phone TEXT NOT NULL, snapshot_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL, recovered_at TEXT NOT NULL DEFAULT '', "
        "tenant_id TEXT NOT NULL REFERENCES tenants(id)",
        None,
    ),
    "linkedin_lead_owners": (
        {"lead_id", "user_id", "created_at", "tenant_id"},
        "lead_id INTEGER NOT NULL, user_id TEXT NOT NULL, "
        "created_at TEXT NOT NULL, "
        "tenant_id TEXT NOT NULL REFERENCES tenants(id), "
        "UNIQUE (tenant_id, lead_id, user_id)",
        None,
    ),
}


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _check_integrity(conn: sqlite3.Connection, label: str) -> None:
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise ValueError(f"{label}: integrity_check failed: {result}")


def _objects(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()


def _rebuild_phone_tenant_keys(conn: sqlite3.Connection, table: str) -> None:
    """Replace legacy user-only keys on the offline candidate, preserving IDs."""
    expected = _SAVED_COLUMNS if table == "phone_user_leads" else _LIMIT_COLUMNS
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({_quoted(table)})")}
    if columns != expected:
        raise ValueError(f"{table} has unrecognized columns")
    custom = conn.execute(
        "SELECT name FROM sqlite_master WHERE tbl_name = ? "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    if custom:
        raise ValueError(f"{table} has custom schema objects")
    before = conn.execute(f"SELECT COUNT(*) FROM {_quoted(table)}").fetchone()[0]
    new_table = f"{table}_tenant_new"
    if table == "phone_user_leads":
        old_sequence = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,),
        ).fetchone()
        conn.execute(f"""
            CREATE TABLE {_quoted(new_table)} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                phone TEXT NOT NULL,
                person_name TEXT NOT NULL DEFAULT '',
                business_name TEXT NOT NULL DEFAULT '',
                trade TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                license_status TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                email_source TEXT NOT NULL DEFAULT '',
                website TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'contact',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                UNIQUE (tenant_id, user_id, phone, kind)
            )
        """)
    else:
        old_sequence = None
        conn.execute(f"""
            CREATE TABLE {_quoted(new_table)} (
                user_id TEXT NOT NULL,
                daily_limit INTEGER NOT NULL CHECK (daily_limit >= 0),
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                PRIMARY KEY (tenant_id, user_id)
            )
        """)
    fields = ", ".join(_quoted(column) for column in (
        ("id", "user_id", "phone", "person_name", "business_name", "trade",
         "city", "state", "source", "source_url", "license_status", "email",
         "email_source", "website", "kind", "note", "created_at", "updated_at",
         "tenant_id")
        if table == "phone_user_leads" else
        ("user_id", "daily_limit", "tenant_id")
    ))
    conn.execute(
        f"INSERT INTO {_quoted(new_table)} ({fields}) "
        f"SELECT {fields} FROM {_quoted(table)}"
    )
    if conn.execute(f"SELECT COUNT(*) FROM {_quoted(new_table)}").fetchone()[0] != before:
        raise ValueError(f"{table} row count changed during key migration")
    conn.execute(f"DROP TABLE {_quoted(table)}")
    conn.execute(f"ALTER TABLE {_quoted(new_table)} RENAME TO {_quoted(table)}")
    if old_sequence is not None:
        updated = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?",
            (old_sequence[0], table),
        )
        if updated.rowcount == 0:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                (table, old_sequence[0]),
            )


def _rebuild_workflow_table(conn: sqlite3.Connection, table: str) -> None:
    """Remove implicit first-tenant defaults from an offline private table."""
    expected, definition, allowed_index = _WORKFLOW_SCHEMAS[table]
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({_quoted(table)})")]
    if set(columns) != expected:
        raise ValueError(f"{table} has unrecognized columns")
    objects = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = ? "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    indexes = []
    for kind, name, sql in objects:
        if kind != "index" or name != allowed_index:
            raise ValueError(f"{table} has custom schema objects")
        index_columns = [
            row[2] for row in conn.execute(f"PRAGMA index_info({_quoted(name)})")
        ]
        if index_columns != ["user_id", "created_at"]:
            raise ValueError(f"{table} has changed index schema")
        indexes.append(sql)
    if allowed_index is not None and len(indexes) != 1:
        raise ValueError(f"{table} is missing its known index")
    before = conn.execute(f"SELECT COUNT(*) FROM {_quoted(table)}").fetchone()[0]
    sequence = None
    if "id" in expected:
        sequence_row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,),
        ).fetchone()
        sequence = sequence_row[0] if sequence_row is not None else None
    new_table = f"{table}_tenant_new"
    conn.execute(f"CREATE TABLE {_quoted(new_table)} ({definition})")
    fields = ", ".join(_quoted(column) for column in columns)
    conn.execute(
        f"INSERT INTO {_quoted(new_table)} ({fields}) "
        f"SELECT {fields} FROM {_quoted(table)}"
    )
    if conn.execute(f"SELECT COUNT(*) FROM {_quoted(new_table)}").fetchone()[0] != before:
        raise ValueError(f"{table} row count changed during tenant migration")
    conn.execute(f"DROP TABLE {_quoted(table)}")
    conn.execute(f"ALTER TABLE {_quoted(new_table)} RENAME TO {_quoted(table)}")
    for sql in indexes:
        conn.execute(sql)
    if sequence is not None:
        updated = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?",
            (sequence, table),
        )
        if updated.rowcount == 0:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                (table, sequence),
            )


def _rebuild_email_accounts(conn: sqlite3.Connection) -> None:
    """Give copied credentials an explicit tenant key without touching tokens."""
    table = "email_accounts"
    columns = [row[1] for row in conn.execute("PRAGMA table_info(email_accounts)")]
    if set(columns) != _EMAIL_ACCOUNT_COLUMNS:
        raise ValueError("email_accounts has unrecognized columns")
    custom = conn.execute(
        "SELECT name FROM sqlite_master WHERE tbl_name = 'email_accounts' "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
    ).fetchall()
    if custom:
        raise ValueError("email_accounts has custom schema objects")
    before = conn.execute("SELECT COUNT(*) FROM email_accounts").fetchone()[0]
    sequence_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'email_accounts'"
    ).fetchone()
    conn.execute("""
        CREATE TABLE email_accounts_tenant_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'google',
            email TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            access_token TEXT NOT NULL DEFAULT '',
            refresh_token TEXT NOT NULL DEFAULT '',
            token_expires_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'connected',
            scopes TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            UNIQUE (tenant_id, user_id, provider, email)
        )
    """)
    fields = ", ".join(_quoted(column) for column in columns)
    conn.execute(
        f"INSERT INTO email_accounts_tenant_new ({fields}) "
        f"SELECT {fields} FROM email_accounts"
    )
    if conn.execute("SELECT COUNT(*) FROM email_accounts_tenant_new").fetchone()[0] != before:
        raise ValueError("email_accounts row count changed during tenant migration")
    conn.execute("DROP TABLE email_accounts")
    conn.execute("ALTER TABLE email_accounts_tenant_new RENAME TO email_accounts")
    if sequence_row is not None:
        updated = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'email_accounts'",
            (sequence_row[0],),
        )
        if updated.rowcount == 0:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('email_accounts', ?)",
                (sequence_row[0],),
            )


def _rebuild_campaigns(conn: sqlite3.Connection) -> None:
    """Remove the temporary first-tenant default from campaign owners."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(campaigns)")]
    if set(columns) != _CAMPAIGN_COLUMNS:
        raise ValueError("campaigns has unrecognized columns")
    custom = conn.execute(
        "SELECT name FROM sqlite_master WHERE tbl_name = 'campaigns' "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
    ).fetchall()
    if custom:
        raise ValueError("campaigns has custom schema objects")
    before = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    sequence_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'campaigns'"
    ).fetchone()
    conn.execute("""
        CREATE TABLE campaigns_tenant_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            account_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            paused_reason TEXT NOT NULL DEFAULT '',
            resume_at TEXT NOT NULL DEFAULT '',
            start_at TEXT NOT NULL,
            daily_limit INTEGER NOT NULL DEFAULT 30,
            delay_min_s INTEGER NOT NULL DEFAULT 180,
            delay_max_s INTEGER NOT NULL DEFAULT 420,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ai_personalize INTEGER NOT NULL DEFAULT 0,
            tenant_id TEXT NOT NULL REFERENCES tenants(id)
        )
    """)
    fields = ", ".join(_quoted(column) for column in columns)
    conn.execute(
        f"INSERT INTO campaigns_tenant_new ({fields}) SELECT {fields} FROM campaigns"
    )
    if conn.execute("SELECT COUNT(*) FROM campaigns_tenant_new").fetchone()[0] != before:
        raise ValueError("campaigns row count changed during tenant migration")
    conn.execute("DROP TABLE campaigns")
    conn.execute("ALTER TABLE campaigns_tenant_new RENAME TO campaigns")
    if sequence_row is not None:
        updated = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'campaigns'",
            (sequence_row[0],),
        )
        if updated.rowcount == 0:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('campaigns', ?)",
                (sequence_row[0],),
            )


def build_unified_database(
    sources: list[Path], target: Path,
) -> dict[str, dict[str, int]]:
    """Snapshot and merge distinct SQLite schemas; publish only after checks.

    All source connections are read-only. A target that already exists is
    never replaced, even if a concurrent process creates it during the build.
    """
    sources = [Path(path).resolve() for path in sources]
    target = Path(target).resolve()
    if not sources or len(sources) != len(set(sources)):
        raise ValueError("provide at least one distinct source database")
    if len({path.name for path in sources}) != len(sources):
        raise ValueError("source basenames must be distinct for the manifest")
    if target.exists():
        raise FileExistsError(target)
    for path in sources:
        if path == target or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"source missing or empty: {path}")

    target.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, dict[str, int]] = {}
    with TemporaryDirectory(prefix="unified-db-", dir=target.parent) as temp:
        temp_dir = Path(temp)
        snapshots: list[tuple[Path, Path, list[tuple[str, str, str]]]] = []
        seen_names: dict[str, str] = {}
        for number, source in enumerate(sources):
            snapshot = temp_dir / f"source-{number}.db"
            with closing(_read_only(source)) as live, closing(sqlite3.connect(snapshot)) as copy:
                live.backup(copy)
                _check_integrity(copy, source.name)
            with closing(_read_only(snapshot)) as copy:
                objects = _objects(copy)
                for _, name, sql in objects:
                    if sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
                        raise ValueError(f"virtual table needs explicit migration: {name}")
                    if name in seen_names:
                        raise ValueError(
                            f"schema object collision: {name} in "
                            f"{seen_names[name]} and {source.name}"
                        )
                    seen_names[name] = source.name
                    if name in _RESERVED_TABLES:
                        raise ValueError(f"source uses reserved unified table: {name}")
                counts[source.name] = {}
            snapshots.append((source, snapshot, objects))

        candidate = temp_dir / "candidate.db"
        with closing(sqlite3.connect(candidate)) as merged:
            merged.execute("PRAGMA foreign_keys=OFF")
            with merged:
                for _, _, objects in snapshots:
                    for kind, _, sql in objects:
                        if kind == "table":
                            merged.execute(sql)

                for source, snapshot, objects in snapshots:
                    with closing(_read_only(snapshot)) as copy:
                        for kind, name, _ in objects:
                            if kind != "table":
                                continue
                            columns = [
                                row[1] for row in copy.execute(
                                    f"PRAGMA table_xinfo({_quoted(name)})"
                                ) if row[6] == 0
                            ]
                            if not columns:
                                raise ValueError(f"table has no copyable columns: {name}")
                            fields = ", ".join(_quoted(column) for column in columns)
                            placeholders = ", ".join("?" for _ in columns)
                            query = f"SELECT {fields} FROM {_quoted(name)}"
                            cursor = copy.execute(query)
                            insert = (
                                f"INSERT INTO {_quoted(name)} ({fields}) "
                                f"VALUES ({placeholders})"
                            )
                            while batch := cursor.fetchmany(1000):
                                merged.executemany(insert, batch)
                            source_count = copy.execute(
                                f"SELECT COUNT(*) FROM {_quoted(name)}"
                            ).fetchone()[0]
                            target_count = merged.execute(
                                f"SELECT COUNT(*) FROM {_quoted(name)}"
                            ).fetchone()[0]
                            if source_count != target_count:
                                raise ValueError(f"row-count mismatch: {source.name}/{name}")
                            counts[source.name][name] = source_count

                        has_sequence = copy.execute(
                            "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"
                        ).fetchone()
                        if has_sequence:
                            for name, sequence in copy.execute(
                                "SELECT name, seq FROM sqlite_sequence"
                            ):
                                updated = merged.execute(
                                    "UPDATE sqlite_sequence SET seq=? WHERE name=?",
                                    (sequence, name),
                                )
                                if updated.rowcount == 0:
                                    merged.execute(
                                        "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                                        (name, sequence),
                                    )

                for _, _, objects in snapshots:
                    for kind, _, sql in objects:
                        if kind in ("index", "view", "trigger"):
                            merged.execute(sql)
                merged.execute(
                    "CREATE TABLE tenants ("
                    "id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE)"
                )
                merged.execute(
                    "CREATE TABLE tenant_memberships ("
                    "tenant_id TEXT NOT NULL REFERENCES tenants(id), "
                    "user_id TEXT NOT NULL, "
                    "role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')), "
                    "PRIMARY KEY (tenant_id, user_id))"
                )
                merged.execute(
                    "CREATE TABLE unified_metadata (version INTEGER NOT NULL)"
                )
                merged.execute(
                    "INSERT INTO tenants (id, name) VALUES (?, ?)",
                    (FIRST_TENANT_ID, FIRST_TENANT_NAME),
                )
                if "users" in seen_names:
                    columns = {
                        row[1] for row in merged.execute("PRAGMA table_info(users)")
                    }
                    if not {"id", "is_admin"}.issubset(columns):
                        raise ValueError("users table lacks id or is_admin")
                    merged.execute(
                        "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                        "SELECT ?, id, CASE WHEN is_admin = 1 "
                        "THEN 'owner' ELSE 'member' END FROM users",
                        (FIRST_TENANT_ID,),
                    )
                if "activity_log" in seen_names:
                    columns = {
                        row[1] for row in merged.execute(
                            "PRAGMA table_info(activity_log)"
                        )
                    }
                    if not {"user_id", "action"}.issubset(columns):
                        raise ValueError("activity_log lacks user_id or action")
                    if "tenant_id" not in columns:
                        merged.execute(
                            "ALTER TABLE activity_log ADD COLUMN tenant_id TEXT "
                            "REFERENCES tenants(id)"
                        )
                    elif merged.execute(
                        "SELECT 1 FROM activity_log WHERE tenant_id IS NOT NULL LIMIT 1"
                    ).fetchone() is not None:
                        raise ValueError("activity_log already has tenant-owned rows")
                    merged.execute(
                        "UPDATE activity_log SET tenant_id = ?",
                        (FIRST_TENANT_ID,),
                    )
                if "email_accounts" in seen_names:
                    columns = {
                        row[1] for row in merged.execute(
                            "PRAGMA table_info(email_accounts)"
                        )
                    }
                    if "tenant_id" in columns:
                        raise ValueError("email_accounts already has tenant_id")
                    merged.execute(
                        "ALTER TABLE email_accounts ADD COLUMN tenant_id TEXT "
                        "NOT NULL DEFAULT 'the-best-estimators-llc' REFERENCES tenants(id)"
                    )
                    merged.execute(
                        "UPDATE email_accounts SET tenant_id = ?",
                        (FIRST_TENANT_ID,),
                    )
                    _rebuild_email_accounts(merged)
                if "campaigns" in seen_names:
                    columns = {
                        row[1] for row in merged.execute("PRAGMA table_info(campaigns)")
                    }
                    if "tenant_id" in columns:
                        raise ValueError("campaigns already has tenant_id")
                    merged.execute(
                        "ALTER TABLE campaigns ADD COLUMN tenant_id TEXT NOT NULL "
                        "DEFAULT 'the-best-estimators-llc' REFERENCES tenants(id)"
                    )
                    merged.execute(
                        "UPDATE campaigns SET tenant_id = ?", (FIRST_TENANT_ID,)
                    )
                    _rebuild_campaigns(merged)
                # Raw board inventory and global suppressions remain platform
                # owned. Only the caller's private claim/workflow tables get
                # a tenant mapping, and an unknown phone user table fails
                # closed instead of silently shipping unclassified data.
                for kind, name, _ in _objects(merged):
                    if kind != "table" or not name.startswith("phone_"):
                        continue
                    columns = {
                        row[1] for row in merged.execute(
                            f"PRAGMA table_info({_quoted(name)})"
                        )
                    }
                    if name not in _PRIVATE_PHONE_TABLES:
                        if "user_id" in columns:
                            raise ValueError(
                                f"unclassified user-scoped phone table: {name}"
                            )
                        continue
                    if "user_id" not in columns:
                        raise ValueError(f"private phone table lacks user_id: {name}")
                    if "tenant_id" in columns:
                        raise ValueError(f"private phone table already has tenant_id: {name}")
                    merged.execute(
                        f"ALTER TABLE {_quoted(name)} "
                        "ADD COLUMN tenant_id TEXT NOT NULL "
                        "DEFAULT 'the-best-estimators-llc' REFERENCES tenants(id)"
                    )
                    merged.execute(
                        f"UPDATE {_quoted(name)} SET tenant_id = ?",
                        (FIRST_TENANT_ID,),
                    )
                    if merged.execute(
                        f"SELECT 1 FROM {_quoted(name)} "
                        "WHERE tenant_id IS NULL LIMIT 1"
                    ).fetchone() is not None:
                        raise ValueError(f"phone tenant backfill incomplete: {name}")
                for table in ("phone_user_leads", "phone_daily_limits"):
                    if table in seen_names:
                        _rebuild_phone_tenant_keys(merged, table)
                if "linkedin_lead_owners" in seen_names:
                    table = "linkedin_lead_owners"
                    columns = {
                        row[1] for row in merged.execute(
                            f"PRAGMA table_info({_quoted(table)})"
                        )
                    }
                    if "tenant_id" in columns:
                        raise ValueError("linkedin_lead_owners already has tenant_id")
                    merged.execute(
                        f"ALTER TABLE {_quoted(table)} "
                        "ADD COLUMN tenant_id TEXT NOT NULL "
                        "DEFAULT 'the-best-estimators-llc' REFERENCES tenants(id)"
                    )
                    merged.execute(
                        f"UPDATE {_quoted(table)} SET tenant_id = ?",
                        (FIRST_TENANT_ID,),
                    )
                for kind, name, _ in _objects(merged):
                    if kind != "table" or not name.startswith("linkedin_"):
                        continue
                    columns = {
                        row[1] for row in merged.execute(
                            f"PRAGMA table_info({_quoted(name)})"
                        )
                    }
                    if name != "linkedin_lead_owners" and "user_id" in columns:
                        raise ValueError(f"unclassified user-scoped LinkedIn table: {name}")
                for table in _WORKFLOW_SCHEMAS:
                    if table in seen_names:
                        _rebuild_workflow_table(merged, table)
                merged.execute("INSERT INTO unified_metadata (version) VALUES (1)")
            _check_integrity(merged, "unified candidate")
            if merged.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("unified candidate has foreign-key violations")

        # Hard-link publishing fails rather than overwriting a target created
        # while the copy ran. The temporary directory then removes its link.
        os.link(candidate, target)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    counts = build_unified_database(args.source, args.target)
    for source, tables in counts.items():
        print(f"{source}: {len(tables)} tables, {sum(tables.values())} rows")
    print(f"Verified consolidated copy: {args.target}")


if __name__ == "__main__":
    main()
