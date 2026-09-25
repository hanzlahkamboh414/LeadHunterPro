"""The offline consolidation must never modify or silently lose source data."""

import sqlite3

import pytest

from app.auth.models import UserStore
from app.phones.store import PhoneLeadsStore
from scripts.prepare_unified_database import build_unified_database


def _db(path, statements):
    with sqlite3.connect(path) as conn:
        conn.executescript(statements)


def test_merges_tables_indexes_triggers_and_autoincrement_without_touching_sources(tmp_path):
    first = tmp_path / "users.db"
    second = tmp_path / "phones.db"
    target = tmp_path / "unified.db"
    _db(first, """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO users (id, name) VALUES (5, 'Alice');
        DELETE FROM users WHERE id = 5;
        CREATE UNIQUE INDEX idx_users_name ON users (name);
    """)
    _db(second, """
        CREATE TABLE phones (id INTEGER PRIMARY KEY, number TEXT NOT NULL);
        CREATE TABLE phone_audit (number TEXT NOT NULL);
        CREATE TRIGGER phone_insert AFTER INSERT ON phones BEGIN
            INSERT INTO phone_audit (number) VALUES (NEW.number);
        END;
        INSERT INTO phones VALUES (1, '5551000');
    """)

    counts = build_unified_database([first, second], target)

    assert counts == {"users.db": {"users": 0},
                      "phones.db": {"phones": 1, "phone_audit": 1}}
    with sqlite3.connect(target) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='users'").fetchone()[0] == 5
        assert conn.execute("SELECT number FROM phones").fetchone()[0] == "5551000"
        conn.execute("INSERT INTO phones VALUES (2, '5552000')")
        assert conn.execute("SELECT number FROM phone_audit ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "5552000"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='idx_users_name'").fetchone()
    with sqlite3.connect(second) as conn:
        assert conn.execute("SELECT COUNT(*) FROM phones").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM phone_audit").fetchone()[0] == 1


def test_collision_aborts_without_publishing_target(tmp_path):
    first = tmp_path / "one.db"
    second = tmp_path / "two.db"
    target = tmp_path / "unified.db"
    _db(first, "CREATE TABLE duplicate (id INTEGER PRIMARY KEY);")
    _db(second, "CREATE TABLE duplicate (id INTEGER PRIMARY KEY);")

    with pytest.raises(ValueError, match="collision"):
        build_unified_database([first, second], target)

    assert not target.exists()


def test_existing_target_is_never_overwritten(tmp_path):
    source = tmp_path / "one.db"
    target = tmp_path / "unified.db"
    _db(source, "CREATE TABLE sample (id INTEGER PRIMARY KEY);")
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        build_unified_database([source], target)

    assert target.read_bytes() == b"existing"


def test_wal_rows_are_included_in_consistent_backup(tmp_path):
    source = tmp_path / "wal.db"
    target = tmp_path / "unified.db"
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
        writer.execute("INSERT INTO records VALUES (7)")
        writer.commit()
        build_unified_database([source], target)
    finally:
        writer.close()
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT id FROM records").fetchall() == [(7,)]


def test_first_tenant_and_existing_users_are_registered(tmp_path):
    source = tmp_path / "users.db"
    target = tmp_path / "unified.db"
    _db(source, """
        CREATE TABLE users (id TEXT PRIMARY KEY, is_admin INTEGER NOT NULL);
        INSERT INTO users VALUES ('admin-1', 1), ('caller-1', 0);
    """)

    build_unified_database([source], target)

    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT id, name FROM tenants"
        ).fetchall() == [("the-best-estimators-llc", "The Best Estimators LLC")]
        assert conn.execute(
            "SELECT user_id, role FROM tenant_memberships ORDER BY user_id"
        ).fetchall() == [("admin-1", "owner"), ("caller-1", "member")]
        assert conn.execute(
            "SELECT version FROM unified_metadata"
        ).fetchall() == [(1,)]
    with sqlite3.connect(source) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='tenants'"
        ).fetchone()[0] == 0


def test_existing_tenant_table_refuses_ambiguous_merge(tmp_path):
    source = tmp_path / "already_tenanted.db"
    target = tmp_path / "unified.db"
    _db(source, "CREATE TABLE tenants (id TEXT PRIMARY KEY);")

    with pytest.raises(ValueError, match="reserved unified table"):
        build_unified_database([source], target)

    assert not target.exists()


def test_unmappable_users_refuse_publication(tmp_path):
    source = tmp_path / "users.db"
    target = tmp_path / "unified.db"
    _db(source, "CREATE TABLE users (id TEXT PRIMARY KEY);")

    with pytest.raises(ValueError, match="lacks id or is_admin"):
        build_unified_database([source], target)

    assert not target.exists()


def test_phone_private_rows_map_to_first_tenant_without_tenantizing_raw_pool(tmp_path):
    users_db = tmp_path / "users.db"
    phones_db = tmp_path / "phone_leads.db"
    target = tmp_path / "unified.db"
    user_store = UserStore(db_path=str(users_db))
    user = user_store.create("caller", "caller@local.test", "safe-password")
    phone_store = PhoneLeadsStore(db_path=str(phones_db))
    phone_store.add([
        {"phone": "5031110001", "business_name": "First Co", "trade_category": "GENERAL", "state": "WA"},
        {"phone": "5031110002", "business_name": "Second Co", "trade_category": "GENERAL", "state": "WA"},
    ])
    phone_store.set_daily_limit(user.id, 100)
    first, second = phone_store.serve("gc", "WA", "", 2, user.id)
    phone_store.store_contact(first["id"], user.id)
    phone_store.record_call_event(first["id"], user.id, "dialed")
    phone_store.mark_wrong_number(second["id"], user.id)

    counts = build_unified_database([users_db, phones_db], target)
    private = (
        "phone_lead_owners", "phone_user_leads", "phone_claim_events",
        "phone_daily_limits", "phone_call_events", "phone_wrong_archive",
    )
    with sqlite3.connect(target) as conn:
        for table in private:
            columns = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
            assert columns["tenant_id"][3] == 1
            assert columns["tenant_id"][4] is None
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ?",
                ("the-best-estimators-llc",),
            ).fetchone()[0] == counts[phones_db.name][table]
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
        for table in ("phone_leads", "phone_suppressions"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert "tenant_id" not in columns
        assert conn.execute("SELECT version FROM unified_metadata").fetchone() == (1,)
        for table, fields, values in (
            ("phone_lead_owners", "lead_id, user_id, created_at", (99, "caller", "now")),
            ("phone_claim_events", "lead_id, user_id, phone, created_at", (99, "caller", "+15031110001", "now")),
            ("phone_call_events", "user_id, lead_id, phone, action, created_at", ("caller", 99, "+15031110001", "dialed", "now")),
            ("phone_wrong_archive", "user_id, phone, snapshot_json, created_at", ("caller", "+15031110001", "{}", "now")),
        ):
            placeholders = ", ".join("?" for _ in values)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    f"INSERT INTO {table} ({fields}) VALUES ({placeholders})", values,
                )
    with sqlite3.connect(phones_db) as conn:
        assert "tenant_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(phone_call_events)")
        }


def test_unknown_user_scoped_phone_table_refuses_publication(tmp_path):
    source = tmp_path / "phones.db"
    target = tmp_path / "unified.db"
    _db(source, "CREATE TABLE phone_future_private (user_id TEXT NOT NULL);")

    with pytest.raises(ValueError, match="unclassified user-scoped phone table"):
        build_unified_database([source], target)
    assert not target.exists()


def test_previously_tenantized_phone_table_refuses_ambiguous_backfill(tmp_path):
    source = tmp_path / "phones.db"
    target = tmp_path / "unified.db"
    _db(source, "CREATE TABLE phone_call_events (user_id TEXT, tenant_id TEXT);")

    with pytest.raises(ValueError, match="already has tenant_id"):
        build_unified_database([source], target)
    assert not target.exists()


def test_phone_saved_and_limits_have_independent_tenant_keys_without_id_loss(tmp_path):
    source = tmp_path / "phone_leads.db"
    target = tmp_path / "unified.db"
    store = PhoneLeadsStore(db_path=str(source))
    store.add([
        {"phone": "5031110001", "business_name": "First", "trade_category": "GENERAL", "state": "WA"},
        {"phone": "5031110002", "business_name": "Second", "trade_category": "GENERAL", "state": "WA"},
    ])
    first, second = store.serve("gc", "WA", "", 2, "shared-user")
    first_saved = store.store_contact(first["id"], "shared-user")["saved_id"]
    second_saved = store.store_contact(second["id"], "shared-user")["saved_id"]
    assert store.delete_saved(second_saved, "shared-user")
    store.set_daily_limit("shared-user", 125)

    build_unified_database([source], target)

    with sqlite3.connect(target) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO tenants VALUES ('tenant-b', 'Tenant B')")
        assert conn.execute(
            "SELECT id, tenant_id, phone FROM phone_user_leads"
        ).fetchall() == [(first_saved, "the-best-estimators-llc", "+15031110001")]
        assert conn.execute(
            "SELECT tenant_id, user_id, daily_limit FROM phone_daily_limits"
        ).fetchall() == [("the-best-estimators-llc", "shared-user", 125)]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO phone_user_leads (user_id, phone, created_at, updated_at) "
                "VALUES ('shared-user', '+15031110001', 'now', 'now')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO phone_daily_limits (user_id, daily_limit) "
                "VALUES ('shared-user', 200)"
            )
        saved = conn.execute(
            "INSERT INTO phone_user_leads "
            "(tenant_id, user_id, phone, created_at, updated_at) "
            "VALUES ('tenant-b', 'shared-user', '+15031110001', 'now', 'now')"
        )
        assert saved.lastrowid > second_saved
        conn.execute(
            "INSERT INTO phone_daily_limits (tenant_id, user_id, daily_limit) "
            "VALUES ('tenant-b', 'shared-user', 300)"
        )
        assert conn.execute(
            "SELECT tenant_id, daily_limit FROM phone_daily_limits "
            "WHERE user_id='shared-user' ORDER BY tenant_id"
        ).fetchall() == [
            ("tenant-b", 300), ("the-best-estimators-llc", 125),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO phone_user_leads "
                "(tenant_id, user_id, phone, created_at, updated_at) "
                "VALUES ('tenant-b', 'shared-user', '+15031110001', 'now', 'now')"
            )
        assert conn.execute("SELECT version FROM unified_metadata").fetchone() == (1,)
    with sqlite3.connect(source) as conn:
        assert "tenant_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(phone_user_leads)")
        }


def test_unknown_saved_column_prevents_offline_publication(tmp_path):
    source = tmp_path / "phone_leads.db"
    target = tmp_path / "unified.db"
    PhoneLeadsStore(db_path=str(source))
    with sqlite3.connect(source) as conn:
        conn.execute("ALTER TABLE phone_user_leads ADD COLUMN future_private TEXT")

    with pytest.raises(ValueError, match="phone_user_leads.*columns"):
        build_unified_database([source], target)
    assert not target.exists()


def test_private_phone_owner_keys_and_event_indexes_survive_rebuild(tmp_path):
    source = tmp_path / "phone_leads.db"
    target = tmp_path / "unified.db"
    store = PhoneLeadsStore(db_path=str(source))
    store.add([{"phone": "5031110001", "business_name": "First", "trade_category": "GENERAL", "state": "WA"}])
    lead = store.serve("gc", "WA", "", 1, "caller")[0]
    store.record_call_event(lead["id"], "caller", "dialed")

    build_unified_database([source], target)

    with sqlite3.connect(target) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO tenants VALUES ('tenant-b', 'Tenant B')")
        owner = conn.execute(
            "SELECT lead_id, user_id, created_at, batch_at FROM phone_lead_owners"
        ).fetchone()
        conn.execute(
            "INSERT INTO phone_lead_owners "
            "(tenant_id, lead_id, user_id, created_at, batch_at) VALUES (?, ?, ?, ?, ?)",
            ("tenant-b", *owner),
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM phone_lead_owners WHERE lead_id = ? AND user_id = ?",
            owner[:2],
        ).fetchone()[0] == 2
        for table, index in (
            ("phone_claim_events", "idx_phone_claim_day"),
            ("phone_call_events", "idx_phone_call_day"),
        ):
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name=? AND name=?",
                (table, index),
            ).fetchone() is not None
            assert conn.execute(f"SELECT id FROM {table} LIMIT 1").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(source) as conn:
        assert "tenant_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(phone_lead_owners)")
        }


def test_unknown_owner_schema_prevents_offline_publication(tmp_path):
    source = tmp_path / "phone_leads.db"
    target = tmp_path / "unified.db"
    PhoneLeadsStore(db_path=str(source))
    with sqlite3.connect(source) as conn:
        conn.execute("ALTER TABLE phone_lead_owners ADD COLUMN future_private TEXT")

    with pytest.raises(ValueError, match="phone_lead_owners.*columns"):
        build_unified_database([source], target)
    assert not target.exists()


def test_custom_saved_index_prevents_offline_publication(tmp_path):
    source = tmp_path / "phone_leads.db"
    target = tmp_path / "unified.db"
    PhoneLeadsStore(db_path=str(source))
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE INDEX saved_custom ON phone_user_leads (updated_at)")

    with pytest.raises(ValueError, match="phone_user_leads.*custom schema"):
        build_unified_database([source], target)
    assert not target.exists()


def test_legacy_unified_phone_writes_use_only_unambiguous_tenant(tmp_path):
    source = tmp_path / "phone_leads.db"
    target = tmp_path / "unified.db"
    PhoneLeadsStore(db_path=str(source))
    build_unified_database([source], target)
    store = PhoneLeadsStore(db_path=str(target))
    store.set_daily_limit("caller", 1)
    store.add([{"phone": "5031110001", "business_name": "First", "trade_category": "GENERAL", "state": "WA"}])
    lead = store.serve("gc", "WA", "", 1, "caller")[0]
    assert store.daily_limit("caller") == 1
    assert store.store_contact(lead["id"], "caller")
    assert len(store.list_saved("caller")) == 1
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT tenant_id FROM phone_user_leads WHERE user_id = 'caller'"
        ).fetchone() == ("the-best-estimators-llc",)
        conn.execute("INSERT INTO tenants VALUES ('tenant-b', 'Tenant B')")
    with pytest.raises(ValueError, match="ambiguous"):
        store.set_daily_limit("caller", 2)


def test_legacy_unified_call_actions_tag_the_only_tenant(tmp_path):
    source = tmp_path / "phone_leads.db"
    target = tmp_path / "unified.db"
    PhoneLeadsStore(db_path=str(source))
    build_unified_database([source], target)
    store = PhoneLeadsStore(db_path=str(target))
    store.add([
        {"phone": "5031110001", "business_name": "First", "trade_category": "GENERAL", "state": "WA"},
        {"phone": "5031110002", "business_name": "Second", "trade_category": "GENERAL", "state": "WA"},
    ])
    first, second = store.serve("gc", "WA", "", 2, "caller")

    assert store.record_call_event(first["id"], "caller", "dialed")
    assert store.mark_lead(first["id"], "caller")
    assert store.mark_voicemail(second["id"], "caller")
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT DISTINCT tenant_id FROM phone_call_events"
        ).fetchall() == [("the-best-estimators-llc",)]
        assert conn.execute(
            "SELECT tenant_id FROM phone_user_leads WHERE user_id = 'caller'"
        ).fetchall() == [("the-best-estimators-llc",)]


def test_phone_limit_overrides_and_serve_are_tenant_scoped(tmp_path, monkeypatch):
    source = tmp_path / "phone_leads.db"
    target = tmp_path / "unified.db"
    PhoneLeadsStore(db_path=str(source))
    build_unified_database([source], target)
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO tenants VALUES ('tenant-b', 'Tenant B')")
    store = PhoneLeadsStore(db_path=str(target))
    store.add([
        {"phone": "5031110001", "business_name": "First", "trade_category": "GENERAL", "state": "WA"},
        {"phone": "5031110002", "business_name": "Second", "trade_category": "GENERAL", "state": "WA"},
        {"phone": "5031110003", "business_name": "Third", "trade_category": "GENERAL", "state": "WA"},
    ])
    monkeypatch.setenv("LEADHUNTER_MULTI_TENANT_ENABLED", "1")
    store.set_daily_limit("shared-user", 1, tenant_id="the-best-estimators-llc")
    store.set_daily_limit("shared-user", 2, tenant_id="tenant-b")
    assert store.daily_limit("shared-user", tenant_id="tenant-b") == 2
    assert store.daily_limit("shared-user", tenant_id="the-best-estimators-llc") == 1
    assert len(store.serve("gc", "WA", "", 2, "shared-user", tenant_id="the-best-estimators-llc")) == 1
    assert len(store.serve("gc", "WA", "", 3, "shared-user", tenant_id="tenant-b")) == 2
    assert store.daily_remaining("shared-user", tenant_id="tenant-b") == 0
    with pytest.raises(ValueError, match="tenant_id"):
        store.daily_limit("shared-user")
    with pytest.raises(ValueError, match="tenant_id"):
        store.set_daily_limit("shared-user", 3)
    with pytest.raises(ValueError, match="unknown tenant"):
        store.set_daily_limit("shared-user", 3, tenant_id="missing")
