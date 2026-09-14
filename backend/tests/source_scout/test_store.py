"""P9 sub-inc 1 — ScoutStore contracts: the staged-trust lifecycle
(quarantine entry, legal forward edges only, honest retirement),
verdicts as the sole promotion currency, and the known/dead memory the
playbook prompt will read.
"""

from __future__ import annotations

import pytest

from app.source_scout.store import (
    STAGE_MECHANICAL,
    STAGE_PROBATION,
    STATUS_PROMOTED,
    STATUS_PROPOSED,
    STATUS_RETIRED,
    ScoutStore,
)


def _store(tmp_path):
    return ScoutStore(db_path=str(tmp_path / "source_scout.db"))


def _propose(store, source_id="or_license", **kw):
    ok = store.propose(
        source_id=source_id,
        kind="soda",
        name="Oregon CCB Licenses",
        endpoint="https://data.oregon.gov/resource/xyz.json",
        payload=kw.get("payload", {"trade_values": {"gc": ["General"]},
                                   "phone_column": "phone",
                                   "state": "OR"}),
        provenance=kw.get("provenance", "playbook/agnes-2.5-flash"),
    )
    assert ok is True
    return store.get(source_id)


# ---------------------------------------------------------------------------
# quarantine entry
# ---------------------------------------------------------------------------

def test_propose_lands_in_quarantine(tmp_path):
    store = _store(tmp_path)
    row = _propose(store)

    assert row["status"] == STATUS_PROPOSED
    assert row["payload"]["phone_column"] == "phone"
    q = store.list_quarantine()
    assert [r["source_id"] for r in q] == ["or_license"]


def test_propose_is_idempotent_per_source_id(tmp_path):
    store = _store(tmp_path)
    _propose(store)

    assert store.propose(
        "or_license", "soda", "dup", "https://x", None, "",
    ) is False
    # Still exactly one row.
    assert len(store.list_quarantine()) == 1


def test_propose_normalizes_source_id_case(tmp_path):
    store = _store(tmp_path)
    _propose(store, "OR_License")

    assert store.propose(
        "or_license", "soda", "dup", "https://x", None, "",
    ) is False


def test_get_unknown_returns_none(tmp_path):
    assert _store(tmp_path).get("nope") is None


# ---------------------------------------------------------------------------
# the staged-trust happy path
# ---------------------------------------------------------------------------

def test_full_lifecycle_proposed_to_promoted(tmp_path):
    store = _store(tmp_path)
    _propose(store)

    store.mark_verified("or_license")
    store.start_probation("or_license")
    # agnes probation: 7 passes out of 10 checks = 70%.
    for i in range(10):
        store.record_verdict(
            "or_license", STAGE_PROBATION, i < 7, f"check {i}")
    passes, total, rate = store.probation_score("or_license")
    assert (passes, total, rate) == (7, 10, 0.7)

    row = store.promote("or_license")
    assert row["status"] == STATUS_PROMOTED
    assert row["promoted_at"] != ""
    # Promotion leaves the quarantine listing.
    assert store.list_quarantine() == []
    assert [r["source_id"] for r in store.list_status(STATUS_PROMOTED)] \
        == ["or_license"]


def test_verified_at_stamped_not_clobbered_by_probation(tmp_path):
    store = _store(tmp_path)
    _propose(store)

    verified = store.mark_verified("or_license")
    started = store.start_probation("or_license")

    assert verified["verified_at"] != ""
    # Probation must not rewrite the verification stamp.
    assert started["verified_at"] == verified["verified_at"]


# ---------------------------------------------------------------------------
# illegal transitions — no stage may be skipped
# ---------------------------------------------------------------------------

def test_proposed_cannot_skip_to_probation_or_promoted(tmp_path):
    store = _store(tmp_path)
    _propose(store)

    with pytest.raises(ValueError, match="illegal transition"):
        store.start_probation("or_license")
    with pytest.raises(ValueError, match="illegal transition"):
        store.promote("or_license")
    # The row is untouched by the refused advances.
    assert store.get("or_license")["status"] == STATUS_PROPOSED


def test_promoted_is_terminal_forward(tmp_path):
    store = _store(tmp_path)
    _propose(store)
    store.mark_verified("or_license")
    store.start_probation("or_license")
    store.promote("or_license")

    with pytest.raises(ValueError, match="illegal transition"):
        store.mark_verified("or_license")


def test_transition_unknown_source_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="unknown source_id"):
        store.mark_verified("ghost")


# ---------------------------------------------------------------------------
# retirement
# ---------------------------------------------------------------------------

def test_retire_from_promoted_with_honest_reason(tmp_path):
    store = _store(tmp_path)
    _propose(store)
    store.mark_verified("or_license")
    store.start_probation("or_license")
    store.promote("or_license")

    store.retire("or_license", "circuit breaker: 20 consecutive timeouts")
    row = store.get("or_license")
    assert row["status"] == STATUS_RETIRED
    assert "consecutive timeouts" in row["retire_reason"]
    assert store.list_quarantine() == []


def test_retire_from_proposed(tmp_path):
    """A proposal that fails verification retires from quarantine."""
    store = _store(tmp_path)
    _propose(store)

    store.retire("or_license", "mechanical verify: shape mismatch")
    assert store.get("or_license")["status"] == STATUS_RETIRED


def test_retired_source_is_never_resurrected_by_reproposal(tmp_path):
    """The Phase H contract: resurrection is human-only. propose() on a
    retired source_id is a no-op, not a second life."""
    store = _store(tmp_path)
    _propose(store)
    store.retire("or_license", "dead")

    assert store.propose(
        "or_license", "soda", "again", "https://x", None, "",
    ) is False
    assert store.get("or_license")["status"] == STATUS_RETIRED


# ---------------------------------------------------------------------------
# verdicts — the sole promotion currency
# ---------------------------------------------------------------------------

def test_probation_score_ignores_mechanical_verdicts(tmp_path):
    """Verified-only reward: agnes probation is judged on probation
    verdicts alone, never on the mechanical verifier's own numbers."""
    store = _store(tmp_path)
    _propose(store)

    store.record_verdict("or_license", STAGE_MECHANICAL, True, "shape ok")
    store.record_verdict("or_license", STAGE_PROBATION, True, "good rows")
    store.record_verdict("or_license", STAGE_PROBATION, False, "thin")

    passes, total, rate = store.probation_score("or_license")
    assert (passes, total, rate) == (1, 2, 0.5)


def test_probation_score_zero_verdicts(tmp_path):
    store = _store(tmp_path)
    _propose(store)

    assert store.probation_score("or_license") == (0, 0, 0.0)


def test_record_verdict_unknown_source_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="unknown source_id"):
        store.record_verdict("ghost", STAGE_PROBATION, True)


# ---------------------------------------------------------------------------
# known/dead memory
# ---------------------------------------------------------------------------

def test_known_memory_upsert_and_map(tmp_path):
    store = _store(tmp_path)
    store.upsert_known("wa_license", "good", "live 2026-09-13 sweep")
    store.upsert_known("public_searxng", "dead",
                       "public instances 429/HTML — never retry")

    km = store.known_map()
    assert km["wa_license"]["status"] == "good"
    assert km["public_searxng"]["status"] == "dead"

    # Upsert updates, not duplicates.
    store.upsert_known("wa_license", "good", "re-verified later")
    assert len(store.known_map()) == 2
    assert store.known_map()["wa_license"]["reason"] == "re-verified later"


def test_known_memory_rejects_bad_status(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="good.*dead"):
        store.upsert_known("x", "maybe")
