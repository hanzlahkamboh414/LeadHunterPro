"""Validator tests — gate 0 + 5 semantic gates (coverage_engine_v2.md §6).

The adapter carries present-flags ONLY; the validator MEASURES them:
fill_rate is stored on every claimed capability, an under-30% capability
is downgraded but never the whole source, and gates 0/1/2/4/5 decide the
source. estimate never stands in for a measurement (total_rows wins).
"""

from __future__ import annotations

import json

from app.source_scout.store import (
    STATUS_ADAPTER_DRAFT,
    STATUS_PROBATION,
    STATUS_PROBING,
    ScoutStore,
)
from app.source_scout.validator import (
    apply_validation,
    run_validation,
    validate,
)


def _store(tmp_path) -> ScoutStore:
    return ScoutStore(db_path=str(tmp_path / "scout.db"))


def _seed_adapter(store: ScoutStore, source_id="ca_cslb_bulk"):
    store.seed_upsert(
        source_id, seed_domain="phones", state="CA", name="CSLB",
        base_url="https://www.cslb.ca.gov",
        seed_meta={"trade_scope": "all_trades", "priority_rank": 1})
    store.start_probing(source_id)
    store.mark_adapter_draft(source_id)


#: the adapter's view: canonical → source column + present-flag claims
FM = {"company_name": "BusinessName", "phone": "BusinessPhone",
      "state_field": "State"}
CAPS = {"phone": {"present": True}, "email": {"present": False}}


def _sample(n=60, *, name_fill=1.0, phone_fill=1.0, dup_rate=0.0,
            state="CA"):
    """Deterministic dry-run rows; dup rows COPY earlier rows so name
    fill counts are unaffected by the duplication."""
    k_name, k_phone = int(n * name_fill), int(n * phone_fill)
    rows = [
        {"BusinessName": f"Acme {i}" if i < k_name else "",
         "BusinessPhone": f"555-0{i:04d}" if i < k_phone else "",
         "State": state}
        for i in range(n)
    ]
    for j in range(int(n * dup_rate)):
        rows[n - int(n * dup_rate) + j] = dict(rows[j])
    return rows


# ---------------------------------------------------------------------------
# gate unit tests (exit criterion: "gate unit tests")
# ---------------------------------------------------------------------------

def test_gate0_parse_feasibility():
    assert validate("s", [], field_map=FM, adapter_capabilities=CAPS,
                    seed_state="CA").passed is False
    assert validate("s", [1, 2], field_map=FM, adapter_capabilities=CAPS,
                    seed_state="CA").passed is False


def test_gate1_rows_returned_floor():
    r = validate("s", _sample(49), field_map=FM, adapter_capabilities=CAPS,
                 seed_state="CA")
    assert not r.passed
    assert "empty_source" in r.gates["rows_returned"]["reason"]
    assert validate("s", _sample(50), field_map=FM,
                    adapter_capabilities=CAPS, seed_state="CA").passed


def test_gate2_company_name_fill():
    r = validate("s", _sample(60, name_fill=0.94), field_map=FM,
                 adapter_capabilities=CAPS, seed_state="CA")
    assert not r.passed
    assert "field_map wrong" in r.gates["company_name"]["reason"]
    assert validate("s", _sample(60, name_fill=0.95), field_map=FM,
                    adapter_capabilities=CAPS, seed_state="CA").passed


def test_gate3_downgrades_weak_capability_never_the_source():
    r = validate("s", _sample(60, phone_fill=0.29), field_map=FM,
                 adapter_capabilities=CAPS, seed_state="CA")
    assert r.passed  # gate 3 is per-capability — the source survives
    phone = r.capabilities["phone"]
    assert phone["present"] is False
    assert phone["fill_rate"] == round(17 / 60, 4)  # measured, stored
    assert r.gates["capability_fill"]["downgraded"] == ["phone"]


def test_gate3_missing_column_measures_zero_and_downgrades():
    fm = {"company_name": "BusinessName", "state_field": "State"}
    r = validate("s", _sample(60), field_map=fm,
                 adapter_capabilities=CAPS, seed_state="CA")
    assert r.capabilities["phone"] == {"present": False, "fill_rate": 0.0}


def test_gate3_stores_measured_fill_rate_on_pass():
    r = validate("s", _sample(60, phone_fill=0.8), field_map=FM,
                 adapter_capabilities=CAPS, seed_state="CA")
    assert r.capabilities["phone"] == {"present": True, "fill_rate": 0.8}
    # unclaimed capabilities are never measured or pushed numbers
    assert "email" in r.capabilities and "fill_rate" not in \
        r.capabilities["email"]


def test_gate4_jurisdiction_error_on_a_wrong_state_source():
    rows = _sample(60)
    for i in range(30):  # half the rows are another jurisdiction
        rows[i]["State"] = "NY"
    r = validate("s", rows, field_map=FM, adapter_capabilities=CAPS,
                 seed_state="CA")
    assert not r.passed
    assert "jurisdiction_error" in r.gates["jurisdiction"]["reason"]
    assert "NY" in r.gates["jurisdiction"]["reason"]
    assert r.gates["jurisdiction_share"]["seed_share"] == 0.5
    assert r.gates["jurisdiction_share"]["alien_states"] == ["NY"]


def test_gate4_tolerates_a_minority_of_out_of_state_mailing_addresses():
    """Live CSLB B-2 (2026-09-17): 17 of 1,596 rows carry out-of-state
    MAILING addresses while every row is a CA licence — a correct source.
    Strict equality rejected it; a dominant share does not. 57/60 sits
    exactly ON the 95% floor, so this pins the >= boundary too."""
    rows = _sample(60)
    for i in range(3):  # 5% out-of-state mailing addresses
        rows[i]["State"] = "NV"
    r = validate("s", rows, field_map=FM, adapter_capabilities=CAPS,
                 seed_state="CA")
    assert r.passed
    assert r.gates["jurisdiction_share"]["seed_share"] == round(57 / 60, 4)
    assert r.gates["jurisdiction_share"]["alien_states"] == ["NV"]


def test_gate4_mixed_source_bounces_at_the_95_floor():
    """8.3% alien (55/60): a mixed-state source, not mailing noise. It
    cleared the earlier 90% floor — the tightening to 95% is what
    bounces it, so this test is the guard on that decision."""
    rows = _sample(60)
    for i in range(5):
        rows[i]["State"] = "AZ"
    r = validate("s", rows, field_map=FM, adapter_capabilities=CAPS,
                 seed_state="CA")
    assert not r.passed
    assert r.gates["jurisdiction_share"]["seed_share"] == round(55 / 60, 4)
    assert "jurisdiction_error" in r.gates["jurisdiction"]["reason"]


def test_gate4_below_share_floor_is_jurisdiction_error():
    rows = _sample(60)
    for i in range(8):  # 86.7% seed share < 95% floor
        rows[i]["State"] = "AZ"
    r = validate("s", rows, field_map=FM, adapter_capabilities=CAPS,
                 seed_state="CA")
    assert not r.passed
    assert "covers only 86.7%" in r.gates["jurisdiction"]["reason"]


def test_gate4_email_tier_skipped_without_seed_state():
    r = validate("s", _sample(60), field_map=FM, adapter_capabilities=CAPS,
                 seed_state="")
    assert r.passed
    assert "no seed jurisdiction" in r.gates["jurisdiction"]["reason"]


def test_gate4_missing_state_field_is_jurisdiction_error():
    fm = {"company_name": "BusinessName", "phone": "BusinessPhone"}
    r = validate("s", _sample(60), field_map=fm,
                 adapter_capabilities=CAPS, seed_state="CA")
    assert not r.passed
    assert "lacks state_field" in r.gates["jurisdiction"]["reason"]


def test_gate5_duplicate_rate_ceiling():
    r = validate("s", _sample(60, dup_rate=0.25), field_map=FM,
                 adapter_capabilities=CAPS, seed_state="CA")
    assert not r.passed
    assert "dedupe" in r.gates["duplicate_rate"]["reason"]
    assert r.gates["duplicate_rate"]["dup_rate"] == 0.25
    assert validate("s", _sample(60, dup_rate=0.15), field_map=FM,
                    adapter_capabilities=CAPS, seed_state="CA").passed


# ---------------------------------------------------------------------------
# registry wiring (apply / run)
# ---------------------------------------------------------------------------

def test_apply_pass_enters_probation_and_persists_measurements(tmp_path):
    store = _store(tmp_path)
    _seed_adapter(store)
    row, result = run_validation(
        store, "ca_cslb_bulk", _sample(60),
        field_map=FM, adapter_capabilities=CAPS, seed_state="CA",
        total_rows=280_000)
    assert result.passed
    assert row["status"] == STATUS_PROBATION
    got = store.get("ca_cslb_bulk")
    assert got["estimated_rows"] == 280_000  # the measurement, not a guess
    assert json.loads(got["capabilities"]) == result.capabilities
    assert got["last_verified_at"]  # stamped
    assert got["gate_fail_reason"] == ""


def test_apply_fail_bounces_to_probing_with_reason(tmp_path):
    store = _store(tmp_path)
    _seed_adapter(store)
    row, result = run_validation(
        store, "ca_cslb_bulk", _sample(49),  # gate 1 fails
        field_map=FM, adapter_capabilities=CAPS, seed_state="CA")
    assert not result.passed
    assert row["status"] == STATUS_PROBING  # schema_mismatch — rework
    assert "only 49 rows" in row["gate_fail_reason"]


def test_auto_advance_false_persists_without_moving_status(tmp_path):
    store = _store(tmp_path)
    _seed_adapter(store)
    row, result = run_validation(
        store, "ca_cslb_bulk", _sample(60),
        field_map=FM, adapter_capabilities=CAPS, seed_state="CA",
        auto_advance=False)
    assert result.passed
    assert row["status"] == STATUS_ADAPTER_DRAFT  # status untouched
    assert store.get("ca_cslb_bulk")["estimated_rows"] == 60


def test_apply_validation_requires_adapter_draft_for_bounce(tmp_path):
    """A promoted source re-validated inside probation is measured and
    stored but never bounced by the validator (phase-5 drift logic)."""
    store = _store(tmp_path)
    _seed_adapter(store)
    row, good = run_validation(
        store, "ca_cslb_bulk", _sample(60),
        field_map=FM, adapter_capabilities=CAPS, seed_state="CA")
    assert row["status"] == STATUS_PROBATION
    store.promote("ca_cslb_bulk")
    row2, bad = run_validation(
        store, "ca_cslb_bulk", _sample(30),
        field_map=FM, adapter_capabilities=CAPS, seed_state="CA")
    assert not bad.passed
    assert row2["status"] == "promoted"  # measurements stored, status kept
    assert "only 30 rows" in row2["gate_fail_reason"]