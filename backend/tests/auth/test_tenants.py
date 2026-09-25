"""Tenant membership is persisted and revocation takes effect immediately."""

import sqlite3

import pytest

from app.auth.models import UserStore
from scripts.prepare_unified_database import build_unified_database


def _unified_store(tmp_path):
    source = tmp_path / "users.db"
    original = UserStore(db_path=str(source))
    owner = original.ensure_admin()
    member = original.create("tenant_member", "member@local.test", "safe-password")
    target = tmp_path / "unified.db"
    build_unified_database([source], target)
    return UserStore(db_path=str(target)), owner, member, target


def test_first_tenant_memberships_survive_reopening(tmp_path):
    store, owner, member, target = _unified_store(tmp_path)

    assert store.tenant_role(owner.id, "the-best-estimators-llc") == "owner"
    assert store.tenant_role(member.id, "the-best-estimators-llc") == "member"
    assert UserStore(db_path=str(target)).tenant_role(member.id, "the-best-estimators-llc") == "member"


def test_second_tenant_is_distinct_and_revocation_is_immediate(tmp_path):
    store, owner, member, target = _unified_store(tmp_path)
    other_tenant = store.create_tenant("Second Firm")

    assert store.tenant_role(member.id, other_tenant) is None
    store.grant_membership(other_tenant, member.id, "admin")
    assert store.tenant_role(member.id, other_tenant) == "admin"
    assert store.tenant_role(owner.id, other_tenant) is None
    assert store.revoke_membership(other_tenant, member.id) is True
    assert UserStore(db_path=str(target)).tenant_role(member.id, other_tenant) is None
    assert store.tenant_role(member.id, "the-best-estimators-llc") == "member"


def test_membership_rejects_unknown_user_tenant_and_role(tmp_path):
    store, _, member, target = _unified_store(tmp_path)

    with pytest.raises(ValueError, match="tenant does not exist"):
        store.grant_membership("missing", member.id, "member")
    with pytest.raises(ValueError, match="user does not exist"):
        store.grant_membership("the-best-estimators-llc", "missing", "member")
    with pytest.raises(ValueError, match="invalid tenant role"):
        store.grant_membership("the-best-estimators-llc", member.id, "platform_admin")
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tenant_memberships").fetchone()[0] == 2


def test_tenant_account_creation_is_atomic_and_memberships_are_private(tmp_path):
    store, owner, member, target = _unified_store(tmp_path)
    second = store.create_tenant("Second Firm")

    with pytest.raises(ValueError, match="tenant does not exist"):
        store.create("orphan", "orphan@local.test", "safe-password", tenant_id="missing")
    assert store.get_by_username("orphan") is None

    created = store.create(
        "second_caller", "second@local.test", "safe-password", tenant_id=second,
    )
    assert store.list_user_tenants(created.id) == [
        {"id": second, "name": "Second Firm", "role": "member"},
    ]
    assert store.list_user_tenants(member.id) == [
        {"id": "the-best-estimators-llc", "name": "The Best Estimators LLC", "role": "member"},
    ]
    assert store.list_user_tenants(owner.id) == [
        {"id": "the-best-estimators-llc", "name": "The Best Estimators LLC", "role": "owner"},
    ]
    assert UserStore(db_path=str(target)).tenant_role(created.id, second) == "member"
