"""Phase 3 Step 3 — AI intelligence survives the downstream result surface
and the dedup/merge path.

Fully offline and deterministic. Builds on the Step 2 proof: an accepted
``ConnectorResult`` already carries ``verification`` + ``ai`` + ``qualification``
in its ``metadata`` (attach seam verified in Step 2). Step 3 proves those
namespaces survive to the FINAL output of the Execute/discovery surface and the
record-merge dedup, and that rejected / unknown / bridge-fixture records can
never acquire AI anywhere along that path.

Scenarios pinned here:

1-2. Accepted connector result -> AI metadata reaches the final output
     serialized by the Execute/discovery surface (``_company_payload`` used by
     ``/connectors/texas-procurement``); ``verification`` survives unchanged
     and authoritative alongside it.
3-5. Rejected / unknown / bridge-fixture (Tier 4) results -> no AI in the
     final output; verification still labels the record truthfully.
6.   Duplicate records preserve/merge AI metadata additively — in
     ``merge_company_records`` and in ``ConnectorManager._deduplicate``
     (first-seen whole-object wins keeps its metadata untouched); ranking
     keeps the object unchanged too.
7.   Hostile AI keys can never overwrite deterministic verification — in the
     merge and in the serialized output.
8.   JSON serialization / round-trip preserves both namespaces and keeps AI
     isolated from verification.

No network, no second AI system, no change to the Phase 2 verifiers/gate.
"""

from __future__ import annotations

import json

from app.api.v1.connectors import _company_payload
from app.connectors.connector_manager import ConnectorManager
from app.connectors.connector_result import ConnectorResult
from app.engines.verification.acceptance_gate import merge_company_records

# ---------------------------------------------------------------------------
# Deterministic metadata shapes (exactly what Step 2 attaches in production)
# ---------------------------------------------------------------------------

VERIFIED_VERIFICATION = {
    "accepted": True,
    "hard_rejected": False,
    "verification_status": "verified",
    "verification_confidence": 0.92,
    "source_tier": 1,
    "business_type": "contractor",
    "city": "Dallas",
    "state": "TX",
    "location_match": True,
    "field_evidence": [
        {
            "field": "state",
            "value": "TX",
            "source": "official_website",
            "source_url": "https://acme-roofing.example.com",
            "confidence": 0.9,
            "fetched_at": "",
            "is_reliable": True,
        }
    ],
}

AI = {
    "score": 88,
    "deterministic_score": 82,
    "ai_score": 88,
    "qualified": True,
    "reason": "official website + state licensing data present",
    "ai_used": True,
    "error": None,
}

QUALIFICATION = {
    "qualified": True,
    "score": 88,
    "summary": "strong commercial roofing contractor fit",
    "ai_used": True,
    "error": None,
}


def _accepted_metadata() -> dict:
    return {
        "verification_status": "verified",
        "verification_confidence": 0.92,
        "source_tier": 1,
        "gate_accepted": True,
        "verification": dict(VERIFIED_VERIFICATION),
        "ai": dict(AI),
        "qualification": dict(QUALIFICATION),
    }


def _rejected_metadata():
    return {
        "verification_status": "rejected",
        "verification_confidence": 0.0,
        "source_tier": 1,
        "gate_accepted": False,
        "verification": {
            "accepted": False,
            "hard_rejected": True,
            "verification_status": "rejected",
            "verification_confidence": 0.0,
            "source_tier": 1,
            "business_type": "manufacturer",
            "city": "Dallas",
            "state": "TX",
        },
    }


def _unknown_metadata():
    return {
        "verification_status": "unknown",
        "verification_confidence": 0.0,
        "source_tier": 3,
        "gate_accepted": False,
        "verification": {
            "accepted": False,
            "hard_rejected": False,
            "verification_status": "unknown",
            "verification_confidence": 0.0,
            "source_tier": 3,
        },
    }


def _bridge_fixture_metadata():
    """Tier-4 bridge data surfaced only because all live discovery failed."""
    return {
        "verification_status": "unknown",
        "verification_confidence": 0.0,
        "source_tier": 4,
        "gate_accepted": False,
        "_discovery_source": "fixture_bridge",
        "verification": {
            "accepted": False,
            "hard_rejected": False,
            "verification_status": "unknown",
            "verification_confidence": 0.0,
            "source_tier": 4,
        },
    }


def _result(
    name="Acme Roofing LLC",
    website="https://acme-roofing.example.com",
    metadata=None,
) -> ConnectorResult:
    return ConnectorResult(
        company_name=name,
        website=website,
        city="Dallas",
        state="TX",
        country="USA",
        source="texas_procurement",
        source_url="https://dir.example.com/acme",
        confidence=0.9,
        metadata=dict(metadata or {}),
    )


# ---------------------------------------------------------------------------
# 1-2. Accepted result -> AI reaches the final output; verification intact
# ---------------------------------------------------------------------------


def test_accepted_result_ai_reaches_final_output_and_verification_survives():
    result = _result(metadata=_accepted_metadata())

    payload = _company_payload(result)

    # AI intelligence reached the serialized surface, additive-only.
    assert payload["ai"]["qualified"] is True
    assert payload["ai"]["ai_used"] is True
    assert payload["ai"]["ai_score"] == 88
    assert payload["qualification"]["qualified"] is True
    # Verification is authoritative and unchanged — evidence intact.
    assert payload["verification_status"] == "verified"
    assert payload["verification_confidence"] == 0.92
    assert payload["gate_accepted"] is True
    assert payload["verification"]["accepted"] is True
    assert payload["verification"]["business_type"] == "contractor"
    assert any(
        e["source_url"] == "https://acme-roofing.example.com"
        for e in payload["verification"]["field_evidence"]
    )
    # Legacy surface keys are untouched.
    assert payload["company_name"] == "Acme Roofing LLC"
    assert payload["source_url"] == "https://dir.example.com/acme"


def test_final_wire_maps_each_accepted_result_additively():
    """The route builds ``[payload(c) for c in companies]`` — here the shape."""
    results = [
        _result(
            name="Acme Roofing LLC",
            website="https://acme-roofing.example.com",
            metadata=_accepted_metadata(),
        ),
        _result(
            name="Bexar Electric Co",
            website="https://bexar-electric.example.com",
            metadata={
                **_accepted_metadata(),
                "verification": {
                    **_accepted_metadata()["verification"],
                    "verification_status": "partially_verified",
                    "accepted": True,
                },
                "verification_status": "partially_verified",
            },
        ),
    ]
    companies = [_company_payload(r) for r in results]

    assert len(companies) == 2
    assert companies[0]["ai"]["qualified"] is True
    assert companies[1]["ai"]["qualified"] is True
    assert companies[1]["verification_status"] == "partially_verified"
    assert companies[1]["verification"]["accepted"] is True


# ---------------------------------------------------------------------------
# 3-5. Rejected / unknown / bridge results can never acquire AI
# ---------------------------------------------------------------------------


def test_rejected_result_has_no_ai_at_final_output():
    payload = _company_payload(_result(metadata=_rejected_metadata()))

    assert payload["ai"] == {}
    assert payload["qualification"] == {}
    assert payload["gate_accepted"] is False
    assert payload["verification_status"] == "rejected"
    assert payload["verification"]["accepted"] is False
    assert payload["verification"]["hard_rejected"] is True


def test_unknown_result_has_no_ai_at_final_output():
    payload = _company_payload(_result(metadata=_unknown_metadata()))

    assert payload["ai"] == {}
    assert payload["qualification"] == {}
    assert payload["gate_accepted"] is False
    assert payload["verification_status"] == "unknown"
    assert payload["verification"]["accepted"] is False


def test_bridge_fixture_has_no_ai_at_final_output():
    """Tier-4 bridge records stay labeled bridge data — no AI augmentation."""
    result = _result(metadata=_bridge_fixture_metadata())

    payload = _company_payload(result)

    assert payload["ai"] == {}
    assert payload["qualification"] == {}
    assert payload["gate_accepted"] is False
    assert payload["verification_status"] == "unknown"
    assert payload["verification"]["source_tier"] == 4
    assert payload["verification"]["accepted"] is False
    # Bridge label itself is not hidden either.
    assert result.metadata.get("_discovery_source") == "fixture_bridge"


# ---------------------------------------------------------------------------
# 6. Duplicate records preserve / merge AI metadata additively
# ---------------------------------------------------------------------------


def test_merge_company_records_preserves_ai_additively():
    """Duplicate-only AI keys survive the merge; primary keys stay authoritative."""
    primary = {
        "company_name": "Acme Roofing LLC",
        "city": "Dallas",
        "state": "TX",
        "verification_status": "verified",
        "verification_confidence": 0.92,
        "ai": dict(AI),
        "qualification": dict(QUALIFICATION),
    }
    duplicate = {
        "company_name": "Acme Roofing LLC",
        "city": "Dallas",
        "state": "TX",
        "source_url": "https://dir.example.com/license/1",
        "ai": {
            "score": 91,
            "ai_score": 91,
            "qualified": True,
            "insight": "holds-augmented-license",
            "refinement_only": True,
            "ai_used": True,
            "error": None,
        },
        "qualification": dict(QUALIFICATION),
    }

    merged = merge_company_records(primary, duplicate)

    # Both namespaces survive the merge additively.
    assert merged["ai"]["qualified"] is True
    assert merged["ai"]["insight"] == "holds-augmented-license"
    assert merged["qualification"]["qualified"] is True
    # Primary stays authoritative on shared/conflicting keys.
    assert merged["ai"]["ai_score"] == 88
    # AI stayed inside its namespace — no top-level leak.
    assert "insight" not in merged
    # Deterministic verification is NEVER overwritten by AI payloads.
    assert merged["verification_status"] == "verified"
    assert merged["city"] == "Dallas"


def test_merge_keeps_bridge_namespaces_without_introducing_ai():
    """A bridge duplicate (no ai) merging into an accepted primary keeps
    primary's AI; a primary without AI never gains it from a bridge merge."""
    accepted_primary = {
        "company_name": "Acme Roofing LLC",
        "verification_status": "verified",
        "ai": dict(AI),
        "qualification": dict(QUALIFICATION),
    }
    bridge_duplicate = {
        "company_name": "Acme Roofing LLC",
        "_discovery_source": "fixture_bridge",
        "source_tier": 4,
        "verification_status": "unknown",
    }
    merged = merge_company_records(accepted_primary, bridge_duplicate)
    assert merged["ai"]["qualified"] is True  # primary AI kept

    # And a bridge primary cannot gain AI from a bridge duplicate.
    bridge_dup2 = {**bridge_duplicate, "ai": {}}
    merged2 = merge_company_records(bridge_duplicate, bridge_dup2)
    assert merged2.get("ai", {}) == {}


def test_manager_dedup_first_seen_keeps_ai_and_verification():
    """First-(domain, name) wins and keeps its entire metadata object — the
    metadata (ai + qualification + verification) survives the dedup."""
    winner = _result(name="Acme Roofing LLC", metadata=_accepted_metadata())
    duplicate = _result(
        name="Acme Roofing",
        website="https://acme-roofing.example.com/site",
        metadata=_accepted_metadata(),
    )
    manager = ConnectorManager()

    deduped = manager._deduplicate([winner, duplicate])

    assert len(deduped) == 1
    assert deduped[0].company_name == "Acme Roofing LLC"
    assert deduped[0].metadata["ai"]["qualified"] is True
    assert deduped[0].metadata["verification"]["accepted"] is True
    assert deduped[0].metadata["qualification"]["qualified"] is True


def test_manager_rank_keeps_object_and_metadata_unchanged():
    manager = ConnectorManager()
    result = _result(metadata=_accepted_metadata())

    ranked = manager._rank_results([result], "Roofing", "Dallas TX")

    assert ranked[0] is result or ranked[0].metadata == result.metadata
    assert ranked[0].metadata["ai"]["qualified"] is True
    assert ranked[0].metadata["gate_accepted"] is True


# ---------------------------------------------------------------------------
# 7. Hostile AI keys cannot overwrite deterministic verification
# ---------------------------------------------------------------------------


def test_hostile_ai_keys_cannot_overwrite_verification_on_merge():
    duplicate = {
        "company_name": "Acme Roofing LLC",
        "ai": {
            "qualified": True,
            "ai_scope": "try",
            "verification_status": "rejected",  # hostile
            "accepted": False,  # hostile
            "city": "Houston",  # hostile location
            "state": "CA",
            "field_evidence": [{"field": "state", "value": "CA"}],  # hostile
        },
    }
    primary = {
        "company_name": "Acme Roofing LLC",
        "city": "Dallas",
        "state": "TX",
        "verification_status": "verified",
        "verification_confidence": 0.92,
        "field_evidence": [
            {"field": "state", "value": "TX", "source": "official_website"}
        ],
        "ai": dict(AI),
    }

    merged = merge_company_records(primary, duplicate)

    # Verification authorities are untouched by the hostile AI.
    assert merged["verification_status"] == "verified"
    assert merged["city"] == "Dallas"
    assert merged["state"] == "TX"
    # The hostile keys stayed inside the `ai` namespace; they never leaked
    # into top-level verification fields.
    assert merged["ai"]["verification_status"] == "rejected"  # harmless, isolated
    assert merged["field_evidence"] == primary["field_evidence"]
    assert len(merged["field_evidence"]) == 1
    assert merged["ai"]["ai_score"] == 88


def test_hostile_ai_keys_isolated_in_serialized_output():
    hostile_metadata = _accepted_metadata()
    hostile_metadata["ai"]["verification_status"] = "rejected"
    hostile_metadata["ai"]["accepted"] = False
    hostile_metadata["ai"]["city"] = "Nowhere"

    payload = _company_payload(_result(metadata=hostile_metadata))

    # Hostile values stay locked inside the ai namespace.
    assert payload["ai"]["verification_status"] == "rejected"
    # Verification data comes only from the verification namespace/authority.
    assert payload["verification_status"] == "verified"
    assert payload["verification"]["accepted"] is True
    assert payload["verification"]["city"] == "Dallas"


# ---------------------------------------------------------------------------
# 8. JSON round-trip preserves both namespaces, AI isolated
# ---------------------------------------------------------------------------


def test_json_roundtrip_preserves_verification_and_ai_namespaces():
    payload = _company_payload(_result(metadata=_accepted_metadata()))

    loaded = json.loads(json.dumps(payload))

    assert loaded["ai"]["qualified"] is True
    assert loaded["ai"]["ai_score"] == 88
    assert loaded["qualification"]["qualified"] is True
    assert loaded["verification"]["accepted"] is True
    assert loaded["verification"]["verification_status"] == "verified"
    assert any(
        e["source_url"] == "https://acme-roofing.example.com"
        for e in loaded["verification"]["field_evidence"]
    )
    # AI keys never appear inside the verification dict.
    for hostile in ("accepted", "verification_status", "field_evidence"):
        assert hostile not in loaded["ai"]
    assert loaded["gate_accepted"] is True