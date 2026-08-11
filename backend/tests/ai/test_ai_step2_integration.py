"""Phase 3 Step 2 — Connector → AcceptanceGate → AIEngine.intelligence_for.

Fully offline and deterministic. Proves the production integration seam
chosen in Phase 3 Step 2: the ONLY place the repo computes
``gate.accepted is True`` is the Step-4 loop of
``TexasProcurementConnector.search``, which already attaches gate verdict as
additive ``verification`` metadata on a frozen ``ConnectorResult``. Step 2
adds, right after that merge, the ``_attach_ai_intelligence`` hook that calls
the already-verified ``AIEngine.intelligence_for``.

Seven scenarios are pinned here:

1-2. Accepted (verified / partially_verified) records get ``ai`` +
     ``qualification`` metadata attached additively; ``verification`` and
     ``gate_accepted`` survive unchanged.
3-4. Rejected / unknown records are NEVER sent to AI (an AI engine that would
     raise is never consulted).
5.   Fixture bridge records (surfaced only because all live discovery failed)
     are never AI-augmented — AI works on accepted live leads only.
6.   AI failure returns deterministic fallback metadata (``ai_used=False``,
     ``error`` set) and never changes the acceptance verdict.
7.   The final serialized per-record metadata keeps verification + evidence
     intact and AI data isolated in its own namespace.

No network, no second AI system, no changes to the Phase 2 verifiers/gate.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from app.connectors.connector_result import ConnectorResult
from app.connectors.texas_procurement import TexasProcurementConnector
from app.engines.ai_engine import AIEngine
from app.engines.verification.acceptance_gate import AcceptanceGate
from app.engines.verification.identity_verifier import IdentityVerificationResult
from app.engines.verification.industry_verifier import IndustryVerificationResult
from app.engines.verification.location_verifier import LocationVerificationResult
from app.engines.verification.models import FieldEvidence, VerificationStatus

INDUSTRY = "Roofing"
LOCATION = "Dallas Texas"

QUALIFIED_RESPONSE = (
    '{"score": 95, "qualified": true, "qualification": "strong fit",'
    ' "reasons": ["roofing relevance"], "strengths": ["contractor"],'
    ' "concerns": []}'
)

#: A record the deterministic gate fully verifies (contractor + official +
#: verified location).
ACCEPTED_RECORD = {
    "company_name": "Acme Roofing LLC",
    "website": "https://acme-roofing.example.com",
    "trade_category": "roofing",
    "industry_focus": "commercial roofing contractor",
    "city": "Dallas",
    "state": "TX",
}


@pytest.fixture
def engine() -> AIEngine:
    """AIEngine with a fake gateway (no real AI client)."""
    with (
        patch("app.ai.scorer.AIGateway") as scorer_gw,
        patch("app.ai.summarizer.AIGateway") as _summarizer_gw,
    ):
        scorer_gw.return_value.ask.return_value = QUALIFIED_RESPONSE
        yield AIEngine()


@pytest.fixture
def failing_engine() -> AIEngine:
    """AIEngine whose gateway raises — proves short-circuit + fallback."""
    with (
        patch("app.ai.scorer.AIGateway") as scorer_gw,
        patch("app.ai.summarizer.AIGateway") as _summarizer_gw,
    ):
        scorer_gw.return_value.ask.side_effect = RuntimeError(
            "omniroute unavailable"
        )
        yield AIEngine()


# ---------------------------------------------------------------------------
# Deterministic verification result factories (reuse Phase 2 models)
# ---------------------------------------------------------------------------


def _identity(
    *,
    website_status="unknown",
    identity_status="unknown",
    official=False,
    name_verified=False,
    confidence=0.0,
    reasons=(),
):
    return IdentityVerificationResult(
        identity_status=VerificationStatus(identity_status),
        website_status=VerificationStatus(website_status),
        official_site_confirmed=official,
        name_verified=name_verified,
        domain_overlap=1.0 if official else 0.0,
        confidence=confidence,
        reasons=list(reasons),
        evidence=[],
        normalized_website="",
    )


def _industry(
    *,
    business_type="contractor",
    status="verified",
    confidence=0.85,
):
    return IndustryVerificationResult(
        business_type=business_type,
        industry_match=business_type == "contractor",
        verification_status=VerificationStatus(status),
        trade_category="roofing",
        confidence=confidence,
        reasons=[],
        evidence=[],
    )


def _location(
    *,
    city=None,
    state=None,
    status="unknown",
    match=False,
    confidence=0.0,
    evidence=(),
):
    return LocationVerificationResult(
        city=city,
        state=state,
        location_status=VerificationStatus(status),
        location_match=match,
        confidence=confidence,
        reasons=[],
        evidence=list(evidence),
    )


def _gate(*, industry=None, location=None, identity=None):
    return AcceptanceGate().evaluate(
        record=ACCEPTED_RECORD,
        identity=identity if identity is not None else _identity(),
        industry=industry if industry is not None else _industry(),
        location=location if location is not None else _location(),
    )


def _verified_gate():
    return _gate(
        identity=_identity(
            website_status="verified",
            identity_status="verified",
            official=True,
            name_verified=True,
            confidence=1.0,
            reasons=["official website confirmed"],
        ),
        location=_location(
            city="Dallas",
            state="TX",
            status="verified",
            match=True,
            confidence=0.9,
            evidence=[
                FieldEvidence(
                    field="city",
                    value="Dallas",
                    source="licensing_board",
                    source_url="https://tlic.example.gov/license/1",
                    confidence=0.9,
                    is_reliable=True,
                )
            ],
        ),
    )


def _rejected_gate():
    return AcceptanceGate().evaluate(
        record={
            "company_name": "Acme Shingle Manufacturer LLC",
            "description": "roofing shingle manufacturer",
            "website": "",
            "_discovery_source": "directory_crawl",
        },
        identity=_identity(),
        industry=_industry(
            business_type="manufacturer",
            status="rejected",
            confidence=0.0,
        ),
        location=_location(
            city="Dallas", state="TX", status="verified", match=True, confidence=0.9
        ),
    )


# ---------------------------------------------------------------------------
# Production-shape harness: the exact Step-4 attach sequence in search()
# ---------------------------------------------------------------------------


def _connector(engine=None) -> TexasProcurementConnector:
    return TexasProcurementConnector(ai_engine=engine)


def _step4_attach(
    connector: TexasProcurementConnector,
    record: dict,
    gate,
    *,
    industry: str = INDUSTRY,
    location: str = LOCATION,
) -> ConnectorResult:
    """Replicate the Step-4 loop for one record (verification merge + AI hook)."""
    result, reason = connector._build_result(record, industry, {"roof", "roofing"})
    assert isinstance(result, ConnectorResult) and reason
    result = replace(
        result,
        metadata={
            **result.metadata,
            "verification_status": gate.verification_status.value,
            "verification_confidence": gate.verification_confidence,
            "source_tier": gate.source_tier,
            "gate_accepted": gate.accepted,
            "verification": gate.to_dict(),
        },
    )
    return connector._attach_ai_intelligence(result, record, gate, industry, location)


def _has_ai(result: ConnectorResult) -> bool:
    return "ai" in result.metadata and "qualification" in result.metadata


# ---------------------------------------------------------------------------
# 1-2. Accepted records get AI attached
# ---------------------------------------------------------------------------


def test_accepted_verified_record_gets_ai_additively(engine):
    connector = _connector(engine)
    gate = _verified_gate()
    assert gate.accepted is True
    assert gate.verification_status == VerificationStatus.verified

    result = _step4_attach(connector, ACCEPTED_RECORD, gate)

    assert _has_ai(result)
    assert result.metadata["ai"]["qualified"] is True
    assert result.metadata["ai"]["ai_used"] is True
    assert result.metadata["ai"]["ai_score"] == 95
    assert result.metadata["qualification"]["qualified"] is True
    # Verification namespace is untouched and additive.
    assert result.metadata["gate_accepted"] is True
    assert result.metadata["verification_status"] == "verified"
    assert result.metadata["verification"]["accepted"] is True
    assert isinstance(result, ConnectorResult)  # frozen contract preserved


def test_accepted_partially_verified_record_gets_ai(engine):
    connector = _connector(engine)
    # Only industry confirmed (no location/identity) -> partially_verified.
    gate = _gate()
    assert gate.accepted is True
    assert gate.verification_status == VerificationStatus.partially_verified

    result = _step4_attach(connector, ACCEPTED_RECORD, gate)

    assert _has_ai(result)
    assert result.metadata["ai"]["ai_used"] is True
    assert result.metadata["verification"]["accepted"] is True
    assert result.metadata["verification_status"] == "partially_verified"


# ---------------------------------------------------------------------------
# 3-4. Rejected / unknown records NEVER consult AI
# ---------------------------------------------------------------------------


def test_rejected_record_never_gets_ai(failing_engine):
    connector = _connector(failing_engine)
    gate = _rejected_gate()
    assert gate.accepted is False
    assert gate.hard_rejected is True

    # The engine would raise if consulted; a rejected record must short-circuit.
    result = _step4_attach(connector, ACCEPTED_RECORD, gate)

    assert not _has_ai(result)
    assert result.metadata["gate_accepted"] is False
    assert result.metadata["verification_status"] == "rejected"
    # Verification is present and authoritative.
    assert result.metadata["verification"]["accepted"] is False


def test_unknown_record_never_gets_ai(failing_engine):
    connector = _connector(failing_engine)
    gate = AcceptanceGate().evaluate(
        record=ACCEPTED_RECORD,
        identity=_identity(),
        industry=_industry(status="unknown", confidence=0.0),
        location=_location(),
    )
    assert gate.accepted is False
    assert gate.verification_status == VerificationStatus.unknown

    result = _step4_attach(connector, ACCEPTED_RECORD, gate)

    assert not _has_ai(result)
    assert result.metadata["gate_accepted"] is False


# ---------------------------------------------------------------------------
# 5. Fixture bridge data never gets AI
# ---------------------------------------------------------------------------


def test_bridge_fixture_record_never_gets_ai(failing_engine):
    """Tier-4 bridge records stay labeled bridge data — no AI augmentation."""
    connector = _connector(failing_engine)
    record = {
        **ACCEPTED_RECORD,
        "_discovery_source": "fixture_bridge",
        "source_tier": 4,
    }
    gate = AcceptanceGate().evaluate(
        record=record,
        identity=_identity(),
        industry=_industry(),
        location=_location(),
    )
    # Accepted is False (not a live tier) and it is NOT a hard rejection —
    # exactly the ADR-002 carve-out that surfaces it as labeled bridge data.
    assert gate.accepted is False
    assert gate.hard_rejected is False

    result = _step4_attach(connector, record, gate)

    assert not _has_ai(result)
    assert result.metadata["gate_accepted"] is False
    assert "verification" in result.metadata


# ---------------------------------------------------------------------------
# 6. AI failure -> deterministic fallback, acceptance unchanged
# ---------------------------------------------------------------------------


def test_ai_failure_attaches_fallback_and_keeps_acceptance(failing_engine):
    connector = _connector(failing_engine)
    gate = _verified_gate()
    assert gate.accepted is True

    result = _step4_attach(connector, ACCEPTED_RECORD, gate)

    assert _has_ai(result)
    ai = result.metadata["ai"]
    assert ai["ai_used"] is False
    assert ai["error"]
    assert ai["ai_score"] is None
    # The deterministic fallback score (no AI) still populates the score keys.
    assert ai["score"] == ai["deterministic_score"]
    assert result.metadata["qualification"]["ai_used"] is False
    # Deterministic acceptance is unchanged by AI failure.
    assert result.metadata["gate_accepted"] is True
    assert result.metadata["verification_status"] == "verified"


# ---------------------------------------------------------------------------
# 7. Serialization preserves verification + evidence, isolates AI
# ---------------------------------------------------------------------------


def test_serialization_preserves_verification_and_isolates_ai(engine):
    connector = _connector(engine)
    gate = _verified_gate()

    result = _step4_attach(connector, ACCEPTED_RECORD, gate)

    loaded = json.loads(json.dumps(result.metadata))
    verification = loaded["verification"]
    assert verification["accepted"] is True
    assert verification["verification_status"] == "verified"
    assert verification["verification_confidence"] > 0.0
    assert any(e["source"] == "licensing_board" for e in verification["field_evidence"])
    # AI lives in its own namespace — never inside verification.
    assert loaded["ai"]["qualified"] is True
    assert loaded["qualification"]["qualified"] is True
    for hostile in ("accepted", "verification_status", "field_evidence"):
        assert hostile not in loaded["ai"]