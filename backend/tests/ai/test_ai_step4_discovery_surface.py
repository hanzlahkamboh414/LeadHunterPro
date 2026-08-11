"""Phase 3 Step 4 — AI intelligence + verification survive the
``CompanyDiscoveryEngine → CompanyDiscoveryResult → /api/v1/discovery`` path.

Fully offline and deterministic. Builds on Steps 2-3 (the connector already
attaches ``verification`` + ``ai`` + ``qualification`` to its ``ConnectorResult``
metadata). Step 4 proves those namespaces are CARRIED VERBATIM through the
engine's model conversion, the cleaner's dedup rebuild, the validator's
validation rebuild, and finally surfaced by the discovery API serializer
(``_company_payload``) — with the full ``CompanyDiscoveryResult`` pipeline
exercised, not mocked away.

Scenarios pinned here:

1-2. Accepted results -> AI reaches the serialized discovery output;
     ``verification`` survives unchanged and authoritative (evidence intact).
3-5. Rejected / unknown / bridge-fixture (Tier 4) results pass through the
     engine unmodified but NEVER acquire AI; bridge stays labeled.
6.   Cleaner dedup keeps the first-seen record's metadata (additive, whole
     object) — dedup itself does not drop ``ai`` / ``verification``.
7.   Hostile AI keys can never overwrite deterministic verification at the
     serialized output (isolated inside the ``ai`` namespace).
8.   Curated serialization does not leak internal provenance keys and keeps
     the pre-existing response fields intact.
9.   JSON serialization / round-trip preserves both namespaces.
10.  Records with no connector metadata still serialize safely (default empty
     ``metadata``) and the no-companies path still works.

No network (``_is_live_website`` is patched to True only to keep the real
cleaner/validator executes offline), no second AI system, no changes to the
Phase 2 verifiers/gate, no changes to the Texas connector.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.api.v1.discovery import _company_payload
from app.connectors.connector_result import ConnectorResult
from app.engines.discovery.company.company_discovery_engine import (
    CompanyDiscoveryEngine,
)
from app.engines.discovery.company.company_models import CompanyDiscoveryResult

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


def _partially_verified_metadata():
    meta = _accepted_metadata()
    meta["verification_status"] = "partially_verified"
    meta["verification"] = {
        **dict(VERIFIED_VERIFICATION),
        "verification_status": "partially_verified",
        "accepted": True,
        "verification_confidence": 0.5,
    }
    return meta


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
    website="https://acme.example.com",
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
# Offline harness: real engine pipeline, connector step faked at the manager
# ---------------------------------------------------------------------------


class _FakeManager:
    """Stands in for ConnectorManager.discover — returns canned ConnectorResults."""

    def __init__(self, results):
        self._results = list(results)

    def discover(self, *, industry: str, location: str, limit: int):
        return list(self._results), {
            "data_source": "live",
            "total_raw": len(self._results),
        }


def _run_engine(results):
    """Run the real CompanyDiscoveryEngine pipeline offline.

    Only the network call inside the validator is patched; the engine's
    conversion, the cleaner's dedup rebuild and the validator's validation
    rebuild all run for real (that is exactly what Step 4 must prove).
    """
    engine = CompanyDiscoveryEngine()
    engine._manager = _FakeManager(results)
    with patch(
        "app.engines.discovery.company.company_validator._is_live_website",
        return_value=True,
    ):
        return engine.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )


# ---------------------------------------------------------------------------
# 1-2. Accepted -> AI reaches the discovery output; verification intact
# ---------------------------------------------------------------------------


def test_accepted_reaches_serialized_discovery_output():
    results, metrics = _run_engine([_result(metadata=_accepted_metadata())])

    assert len(results) == 1
    r = results[0]
    assert isinstance(r, CompanyDiscoveryResult)
    # The dataclass metadata carried the namespaces through the whole path.
    assert r.metadata["ai"]["qualified"] is True
    assert r.metadata["ai"]["ai_used"] is True
    assert r.metadata["verification"]["accepted"] is True

    payload = _company_payload(r)
    # AI reached the serialized output.
    assert payload["ai"]["qualified"] is True
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
    # Pre-existing surface fields are preserved.
    assert payload["source"] == "texas_procurement"
    assert payload["confidence"] == 1.0  # validator confidence boost applied
    assert payload["discovery_reason"].startswith("Discovered via")


def test_multiple_accepted_all_serialized_additively():
    results, _ = _run_engine(
        [
            _result(metadata=_accepted_metadata()),
            _result(
                name="Bexar Electric Co",
                website="https://bexar.example.com",
                metadata=_partially_verified_metadata(),
            ),
        ]
    )

    payloads = [_company_payload(r) for r in results]
    assert len(payloads) == 2
    assert payloads[0]["ai"]["qualified"] is True
    assert payloads[0]["verification_status"] == "verified"
    assert payloads[1]["ai"]["qualified"] is True
    assert payloads[1]["verification_status"] == "partially_verified"
    assert payloads[1]["verification"]["accepted"] is True


# ---------------------------------------------------------------------------
# 3-5. Rejected / unknown / bridge can never acquire AI
# ---------------------------------------------------------------------------


def test_rejected_never_gets_ai_through_engine():
    results, _ = _run_engine([_result(metadata=_rejected_metadata())])
    payload = _company_payload(results[0])

    assert "ai" not in results[0].metadata
    assert payload["ai"] == {}
    assert payload["qualification"] == {}
    assert payload["gate_accepted"] is False
    assert payload["verification_status"] == "rejected"
    assert payload["verification"]["accepted"] is False
    assert payload["verification"]["hard_rejected"] is True


def test_unknown_never_gets_ai_through_engine():
    results, _ = _run_engine([_result(metadata=_unknown_metadata())])
    payload = _company_payload(results[0])

    assert "ai" not in results[0].metadata
    assert payload["ai"] == {}
    assert payload["qualification"] == {}
    assert payload["gate_accepted"] is False
    assert payload["verification_status"] == "unknown"


def test_bridge_fixture_labeled_and_never_gets_ai_through_engine():
    results, _ = _run_engine([_result(metadata=_bridge_fixture_metadata())])
    r = results[0]
    payload = _company_payload(r)

    # The carried metadata keeps the bridge label (dataclass level).
    assert r.metadata.get("_discovery_source") == "fixture_bridge"
    assert r.metadata.get("source_tier") == 4
    # No AI ever appears, and the label is visible via verification tier.
    assert "ai" not in r.metadata
    assert payload["ai"] == {}
    assert payload["qualification"] == {}
    assert payload["gate_accepted"] is False
    assert payload["verification_status"] == "unknown"
    assert payload["verification"]["source_tier"] == 4
    assert payload["verification"]["accepted"] is False
    # Curation keeps the internal provenance key off the wire.
    assert "_discovery_source" not in payload


# ---------------------------------------------------------------------------
# 6. Cleaner dedup keeps the first-seen record's metadata (whole object)
# ---------------------------------------------------------------------------


def test_cleaner_dedup_preserves_first_seen_ai_and_verification():
    results, _ = _run_engine(
        [
            _result(
                name="Acme Roofing LLC",
                website="https://acme.example.com",
                metadata=_accepted_metadata(),
            ),
            # Same (domain, name) after normalization -> duplicate removed.
            _result(
                name="Acme Roofing",
                website="https://acme.example.com/site",
                metadata=_accepted_metadata(),
            ),
        ]
    )

    assert len(results) == 1
    kept = results[0]
    assert kept.metadata["ai"]["qualified"] is True
    assert kept.metadata["gate_accepted"] is True
    assert kept.metadata["verification"]["accepted"] is True
    payload = _company_payload(kept)
    assert payload["ai"]["qualified"] is True
    assert payload["verification"]["accepted"] is True


# ---------------------------------------------------------------------------
# 7. Hostile AI keys cannot overwrite verification at the serialized output
# ---------------------------------------------------------------------------


def test_hostile_ai_keys_cannot_overwrite_verification_at_output():
    hostile = _accepted_metadata()
    hostile["ai"]["verification_status"] = "rejected"
    hostile["ai"]["accepted"] = False
    hostile["ai"]["city"] = "Nowhere"

    results, _ = _run_engine([_result(metadata=hostile)])
    payload = _company_payload(results[0])

    # Hostile values stay locked inside the ai namespace.
    assert payload["ai"]["verification_status"] == "rejected"
    # Verification comes only from the verification authority.
    assert payload["verification_status"] == "verified"
    assert payload["verification"]["accepted"] is True
    assert payload["verification"]["city"] == "Dallas"
    assert payload["city"] == "Dallas"
    # No hostile key leaked to the top level.
    assert "accepted" not in payload


# ---------------------------------------------------------------------------
# 8. Curation — no internal provenance on the wire, legacy fields kept
# ---------------------------------------------------------------------------


def test_curated_output_does_not_leak_internal_provenance():
    meta = _accepted_metadata()
    meta["data_provenance"] = "fixture:acme"
    meta["_discovery_source"] = "directory_crawl"

    results, _ = _run_engine([_result(metadata=meta)])
    payload = _company_payload(results[0])

    assert "data_provenance" not in payload
    assert "_discovery_source" not in payload
    for key in (
        "company_name",
        "website",
        "city",
        "state",
        "country",
        "source",
        "confidence",
        "source_url",
        "discovery_reason",
    ):
        assert key in payload


# ---------------------------------------------------------------------------
# 9. JSON round-trip preserves both namespaces
# ---------------------------------------------------------------------------


def test_json_roundtrip_preserves_verification_and_ai_namespaces():
    results, _ = _run_engine([_result(metadata=_accepted_metadata())])
    payload = _company_payload(results[0])

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
    for hostile in ("accepted", "verification_status", "field_evidence"):
        assert hostile not in loaded["ai"]
    assert loaded["gate_accepted"] is True


# ---------------------------------------------------------------------------
# 10. No-metadata records and empty discovery still work
# ---------------------------------------------------------------------------


def test_no_metadata_record_serializes_safely_with_empty_ai():
    r = CompanyDiscoveryResult(
        company_name="Acme Roofing LLC",
        website="https://acme.example.com",
        city="Dallas",
        state="TX",
    )
    assert r.metadata == {}

    payload = _company_payload(r)

    assert payload["verification"] == {}
    assert payload["ai"] == {}
    assert payload["qualification"] == {}
    assert payload["gate_accepted"] is False
    assert payload["verification_status"] == ""
    assert r.normalized_website == "https://acme.example.com"


def test_empty_discovery_still_returns_empty_result():
    results, metrics = _run_engine([])

    assert results == []
    assert metrics.total_found == 0