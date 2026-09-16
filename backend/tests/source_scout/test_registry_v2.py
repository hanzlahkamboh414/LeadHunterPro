"""V2 registry — status model + capability-gated serving (Phase 1 exit).

The three exit criteria of Phase 1, tested at the store:
1. seed-list invariants — covered by test_seed_lists.py
2. transition legality — dead is the ONLY permanent state; blocked and
   exhausted re-arm; illegal jumps raise
3. the router gate — phone demand must never bind to a source whose
   capabilities.phone.present is false (the TX-mechanical class dies here)
"""

from __future__ import annotations

import pytest

from app.source_scout.store import (
    STATUS_ADAPTER_DRAFT,
    STATUS_BLOCKED,
    STATUS_DEAD,
    STATUS_EXHAUSTED,
    STATUS_PROBING,
    STATUS_PROMOTED,
    STATUS_UNTRIED,
    ScoutStore,
)


def _store(tmp_path) -> ScoutStore:
    return ScoutStore(db_path=str(tmp_path / "scout.db"))


def _seed_phone(store: ScoutStore, source_id="state_CA_board",
                scope="all_trades", rank=1):
    store.seed_upsert(
        source_id, seed_domain="phones", state="CA",
        name="CSLB", endpoint="https://www.cslb.ca.gov",
        seed_meta={"trade_scope": scope, "priority_rank": rank,
                   "estab": 86763})


def test_migration_adds_v2_columns(tmp_path):
    store = _store(tmp_path)
    row = store.get("does_not_exist")
    assert row is None  # get on missing is None, not a column error
    conn = store._conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
    conn.close()
    for col in ("seed_domain", "state", "base_url", "access_path",
                "fetch_spec", "field_map", "trade_mapping", "capabilities",
                "estimated_rows", "rows_consumed", "next_retry_at",
                "last_fetched_at", "last_verified_at", "gate_fail_reason"):
        assert col in cols


def test_migration_idempotent_on_existing_db(tmp_path):
    path = tmp_path / "scout.db"
    ScoutStore(db_path=str(path))          # first init
    ScoutStore(db_path=str(path))          # second init must not blow up
    ScoutStore(db_path=str(path))          # third, still fine


def test_happy_path_to_promotion(tmp_path):
    store = _store(tmp_path)
    _seed_phone(store)
    assert store.get("state_CA_board")["status"] == STATUS_UNTRIED
    store.start_probing("state_CA_board")
    store.mark_adapter_draft("state_CA_board")
    store.enter_probation("state_CA_board")
    row = store.promote("state_CA_board")
    assert row["status"] == STATUS_PROMOTED
    assert row["promoted_at"]


def test_blocked_is_not_dead_and_re_arms(tmp_path):
    store = _store(tmp_path)
    _seed_phone(store)
    store.start_probing("state_CA_board")
    store.mark_blocked("state_CA_board", "403 F5 WAF on portal",
                       next_retry_at="2099-01-01")
    assert store.get("state_CA_board")["status"] == STATUS_BLOCKED
    assert store.get("state_CA_board")["gate_fail_reason"].startswith("403")
    store.re_arm("state_CA_board")
    assert store.get("state_CA_board")["status"] == STATUS_PROBING
    assert store.get("state_CA_board")["next_retry_at"] == ""


def test_dead_is_terminal_forever(tmp_path):
    store = _store(tmp_path)
    _seed_phone(store)
    store.start_probing("state_CA_board")
    store.mark_dead("state_CA_board", "404 gone")
    assert store.get("state_CA_board")["status"] == STATUS_DEAD
    with pytest.raises(ValueError, match="blocked/exhausted"):
        store.re_arm("state_CA_board")  # dead → probing is illegal


def test_illegal_jump_raises(tmp_path):
    store = _store(tmp_path)
    _seed_phone(store)
    with pytest.raises(ValueError, match="illegal"):
        store.promote("state_CA_board")  # untried → promoted is not an edge


def test_exhausted_re_arms_after_30d(tmp_path):
    store = _store(tmp_path)
    _seed_phone(store)
    store.start_probing("state_CA_board")
    store.mark_adapter_draft("state_CA_board")
    store.enter_probation("state_CA_board")
    store.mark_exhausted("state_CA_board", "dup rate 96% × 3",
                         next_retry_at="2099-01-01")
    assert store.get("state_CA_board")["status"] == STATUS_EXHAUSTED
    store.re_arm("state_CA_board")
    assert store.get("state_CA_board")["status"] == STATUS_PROBING


def test_router_gate_never_binds_false_capability(tmp_path):
    """Phase-1 exit criterion #3: promoted_for_vertical filters on
    capabilities[c].present — a phone-less source is invisible to the
    phone lane even when promoted (the TX-mechanical class of waste)."""
    store = _store(tmp_path)
    _seed_phone(store, source_id="state_CA_board")
    store.seed_upsert(
        "tx_tdlr_mech", seed_domain="phones", state="TX",
        name="TDLR", endpoint="https://www.tdlr.texas.gov",
        seed_meta={"trade_scope": "electrical,mechanical", "priority_rank": 3})
    for sid in ("state_CA_board", "tx_tdlr_mech"):
        store.start_probing(sid)
        store.mark_adapter_draft(sid)
        store.enter_probation(sid)
        store.promote(sid)
    # CA claims a phone capability; TX claims none — the router must
    # bind phone demand to CA only (the TX-mechanical class dies here)
    conn = store._conn()
    conn.execute(
        "UPDATE sources SET capabilities = ? WHERE source_id = ?",
        ('{"phone": {"present": true}, "email": {"present": false}}',
         "state_ca_board"))
    conn.execute(
        "UPDATE sources SET capabilities = ? WHERE source_id = ?",
        ('{"phone": {"present": false}, "email": {"present": false}}',
         "tx_tdlr_mech"))
    conn.commit()
    conn.close()
    by_caps = {r["source_id"]: r for r in
               store.promoted_for_vertical("phone")}
    assert "state_ca_board" in by_caps
    assert "tx_tdlr_mech" not in by_caps


def test_router_gate_includes_email_vertical(tmp_path):
    store = _store(tmp_path)
    store.seed_upsert(
        "overture_places", seed_domain="emails", state="",
        name="Overture", endpoint="https://overturemaps.org",
        seed_meta={"trade_scope": "all_trades"})
    store.start_probing("overture_places")
    store.mark_adapter_draft("overture_places")
    store.enter_probation("overture_places")
    store.promote("overture_places")
    conn = store._conn()
    conn.execute(
        "UPDATE sources SET capabilities = ? WHERE source_id = ?",
        ('{"email": {"present": true, "fill_rate": 0.41}, '
         '"phone": {"present": false}}', "overture_places"))
    conn.commit()
    conn.close()
    emails = store.promoted_for_vertical("email")
    phones = store.promoted_for_vertical("phone")
    assert [r["source_id"] for r in emails] == ["overture_places"]
    assert phones == []


def test_phone_queue_orders_by_rank_and_skips_none(tmp_path):
    store = _store(tmp_path)
    _seed_phone(store, source_id="state_NY_board", scope="none", rank=4)
    _seed_phone(store, source_id="state_WA_board", rank=8)
    _seed_phone(store, source_id="state_CA_board", rank=1)
    store.start_probing("state_WA_board")
    q = store.phone_queue()
    ids = [r["source_id"] for r in q]
    assert ids == ["state_ca_board", "state_wa_board"]  # rank order, NY excluded
    assert all(r["state"] != "NY" for r in q)


def test_reseed_never_resurrects(tmp_path):
    store = _store(tmp_path)
    _seed_phone(store)
    store.start_probing("state_CA_board")
    store.mark_blocked("state_CA_board", "403")
    _seed_phone(store)  # same metadata re-seeded — status must survive
    assert store.get("state_CA_board")["status"] == STATUS_BLOCKED