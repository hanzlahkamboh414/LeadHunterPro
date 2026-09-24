"""V2 registry — status model + capability-gated serving (Phase 1 exit).

The three exit criteria of Phase 1, tested at the store:
1. seed-list invariants — covered by test_seed_lists.py
2. transition legality — dead is the ONLY permanent state; blocked and
   exhausted re-arm; illegal jumps raise
3. the router gate — phone demand must never bind to a source whose
   capabilities.phone.present is false (the TX-mechanical class dies here)
"""

from __future__ import annotations

import json

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
    servable_for,
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


# ---------------------------------------------------------------------------
# the serving gate — the capability claim the CONSUMER must honour too.
#
# promoted_for_vertical gates the registry read; servable_for is the same
# verdict for the lanes that actually fetch (soda.servable_promoted), so a
# promoted source cannot be promised as coverage the lane then fails.
# ---------------------------------------------------------------------------

def _promote(store: ScoutStore, source_id: str,
             capabilities: dict | None = None) -> dict:
    """One source through the V2 ladder to promoted, optionally claiming
    capabilities (the V2 vocabulary — a V1 row never has any)."""
    store.seed_upsert(source_id, seed_domain="phones", state="CA",
                      name="CSLB", endpoint="https://www.cslb.ca.gov",
                      seed_meta={"trade_scope": "all_trades"})
    store.start_probing(source_id)
    store.mark_adapter_draft(source_id)
    store.enter_probation(source_id)
    store.promote(source_id)
    if capabilities is not None:
        conn = store._conn()
        conn.execute(
            "UPDATE sources SET capabilities = ? WHERE source_id = ?",
            (json.dumps(capabilities), source_id))
        conn.commit()
        conn.close()
    return store.get(source_id)


def test_servable_for_treats_silence_as_unknown_never_false(tmp_path):
    """A promoted row with NO capability claim is legacy (the V1 ladder
    never spoke this vocabulary and verified a phone column instead).
    Reading that silence as present:false would invent evidence —
    accuracy Rule 8: no evidence is unknown, not false."""
    store = _store(tmp_path)
    row = _promote(store, "or_ccb_license")
    assert row["capabilities"] == "{}"
    assert servable_for(row, "phone") is True
    # ... and the router gate agrees, so the two reads cannot diverge
    assert [r["source_id"] for r in store.promoted_for_vertical("phone")] == \
        ["or_ccb_license"]


def test_servable_for_refuses_a_false_capability(tmp_path):
    store = _store(tmp_path)
    row = _promote(store, "tx_tdlr_mech",
                   {"phone": {"present": False}, "email": {"present": True}})
    assert servable_for(row, "phone") is False
    assert servable_for(row, "email") is True
    assert store.promoted_for_vertical("phone") == []


def test_servable_for_ignores_unpromoted_rows(tmp_path):
    """Same claim, wrong state: promotion is still required."""
    store = _store(tmp_path)
    _seed_phone(store)
    store.start_probing("state_CA_board")
    store.mark_adapter_draft("state_CA_board")
    store.enter_probation("state_CA_board")
    row = store.get("state_CA_board")
    assert row["status"] == "probation"
    assert servable_for(row, "phone") is False


def test_servable_promoted_is_the_gated_view_of_promoted_payloads(tmp_path):
    """The consumer's map excludes what the lane cannot honestly serve,
    while the raw registry view still shows every promoted row — that
    difference IS the gate (and why the gated reader exists)."""
    store = _store(tmp_path)
    _promote(store, "or_ccb_license")                       # legacy: served
    _promote(store, "wa_lni_board", {"phone": {"present": True}})
    _promote(store, "tx_tdlr_mech", {"phone": {"present": False}})

    assert sorted(store.promoted_payloads()) == [
        "or_ccb_license", "tx_tdlr_mech", "wa_lni_board"]
    assert sorted(store.servable_promoted("phone")) == [
        "or_ccb_license", "wa_lni_board"]


def test_servable_promoted_returns_parsed_payloads(tmp_path):
    store = _store(tmp_path)
    _promote(store, "or_ccb_license", {"phone": {"present": True}})
    payload = store.servable_promoted("phone")["or_ccb_license"]
    assert isinstance(payload, dict)              # parsed, never a JSON str
    assert payload["seed"]["trade_scope"] == "all_trades"