"""Phase 3 Step 1 — AI Intelligence Stage: gate-guarded, additive AI scoring.

Fully offline and deterministic. Proves ``AIEngine.intelligence_for``:

- attaches additive ``ai`` / ``qualification`` metadata ONLY to records the
  deterministic AcceptanceGate accepted (verified OR partially_verified);
- NEVER consults AI for rejected / unknown / unverified records (returns ``{}``
  before any AI call, so AI can never turn rejected/unknown/unverified into
  accepted);
- reuses the existing ``CompanyScorer.qualify`` path (no new scoring
  algorithm);
- keeps deterministic verification authoritative: nothing it returns can
  collide with or overwrite ``verification_status``, ``verification_confidence``,
  ``business_type``, ``city``, ``state``, ``field_evidence``, or the
  acceptance decision;
- on AI failure returns deterministic fallback metadata (``ai_used=False``,
  ``error`` set) and never raises past the gate.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.engines.ai_engine import AIEngine
from app.engines.verification.acceptance_gate import AcceptanceGate
from app.engines.verification.identity_verifier import IdentityVerificationResult
from app.engines.verification.industry_verifier import IndustryVerificationResult
from app.engines.verification.location_verifier import LocationVerificationResult
from app.engines.verification.models import FieldEvidence, VerificationStatus

QUERY: dict = {"industry": "Roofing", "location": "Dallas Texas"}

QUALIFIED_RESPONSE = (
    '{"score": 95, "qualified": true, "qualification": "strong fit",'
    ' "reasons": ["roofing relevance"], "strengths": ["contractor"],'
    ' "concerns": []}'
)


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
    """AIEngine whose gateway raises — deterministic fallback path.

    Also proves AI is never consulted for non-accepted records: if
    ``intelligence_for`` short-circuits before any AI call, the side_effect
    is never raised.
    """
    with (
        patch("app.ai.scorer.AIGateway") as scorer_gw,
        patch("app.ai.summarizer.AIGateway") as _summarizer_gw,
    ):
        scorer_gw.return_value.ask.side_effect = RuntimeError(
            "omniroute unavailable"
        )
        yield AIEngine()


# ---------------------------------------------------------------------------
# Deterministic verification result factories (reuse Phase2 models)
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
    reasons=(),
):
    return IndustryVerificationResult(
        business_type=business_type,
        industry_match=business_type == "contractor",
        verification_status=VerificationStatus(status),
        trade_category="roofing",
        confidence=confidence,
        reasons=list(reasons),
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


def _gate(record: dict, *, identity=None, industry=None, location=None):
    return AcceptanceGate().evaluate(
        record=record,
        identity=identity if identity is not None else _identity(),
        industry=industry if industry is not None else _industry(),
        location=location if location is not None else _location(),
    )


def _verified_gate(record: dict):
    """Fully verified acceptance: contractor + official site + location."""
    return _gate(
        record,
        identity=_identity(
            website_status="verified",
            identity_status="verified",
            official=True,
            name_verified=True,
            confidence=1.0,
            reasons=["official website confirmed"],
        ),
        industry=_industry(),
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


def _manufacturer_gate(record: dict):
    """Deterministic rejection: manufacturer can never be a contractor lead."""
    return _gate(
        record,
        industry=_industry(
            business_type="manufacturer",
            status="rejected",
            confidence=0.0,
            reasons=["manufacturer is not a construction contractor"],
        ),
        location=_location(
            city="Dallas", state="TX", status="verified", match=True, confidence=0.9
        ),
    )


# ---------------------------------------------------------------------------
# 1-2. Accepted records get AI intelligence attached
# ---------------------------------------------------------------------------


def test_verified_accepted_record_gets_ai_attached(engine):
    record = {
        "company_name": "Acme Roofing LLC",
        "_discovery_source": "official_website",
    }
    gate = _verified_gate(record)
    assert gate.accepted is True
    assert gate.verification_status == VerificationStatus.verified

    intelligence = engine.intelligence_for(record=record, gate=gate, query=QUERY)

    assert set(intelligence) == {"ai", "qualification"}
    assert intelligence["ai"]["qualified"] is True
    assert intelligence["ai"]["ai_used"] is True
    assert intelligence["ai"]["ai_score"] == 95
    assert intelligence["qualification"]["qualified"] is True
    assert intelligence["qualification"]["score"] == intelligence["ai"]["score"]
    # Verification namespace untouched — the gate result stays authoritative.
    assert gate.accepted is True
    assert gate.verification_status == VerificationStatus.verified


def test_partially_verified_accepted_record_gets_ai_attached(engine):
    record = {
        "company_name": "Acme Roofing LLC",
        "_discovery_source": "directory_crawl",
    }
    gate = _gate(record)  # only industry confirmed -> partially_verified
    assert gate.accepted is True
    assert gate.verification_status == VerificationStatus.partially_verified

    intelligence = engine.intelligence_for(record=record, gate=gate, query=QUERY)

    assert "ai" in intelligence and "qualification" in intelligence
    assert intelligence["ai"]["ai_used"] is True


# ---------------------------------------------------------------------------
# 3-4. Rejected / unknown records NEVER get AI
# ---------------------------------------------------------------------------


def test_manufacturer_rejected_gets_no_ai(failing_engine):
    record = {
        "company_name": "Acme Shingle Manufacturer LLC",
        "description": "roofing shingle manufacturer",
        "website": "",
        "_discovery_source": "directory_crawl",
    }
    gate = _manufacturer_gate(record)
    assert gate.hard_rejected is True
    assert gate.accepted is False
    assert gate.verification_status == VerificationStatus.rejected

    # The gateway raises if consulted; a rejected record short-circuits before
    # any AI call, so intelligence_for returns {} cleanly and the verdict holds.
    intelligence = failing_engine.intelligence_for(
        record=record, gate=gate, query=QUERY
    )
    assert intelligence == {}
    assert gate.accepted is False
    assert gate.verification_status == VerificationStatus.rejected


def test_unknown_record_gets_no_ai(failing_engine):
    record = {
        "company_name": "Acme Roofing LLC",
        "description": "commercial roofing contractor",
        "website": "",
        "_discovery_source": "directory_crawl",
    }
    gate = _gate(record, industry=_industry(status="unknown"))
    assert gate.accepted is False
    assert gate.verification_status == VerificationStatus.unknown

    intelligence = failing_engine.intelligence_for(
        record=record, gate=gate, query=QUERY
    )
    assert intelligence == {}
    assert gate.accepted is False


# ---------------------------------------------------------------------------
# 5. AI failure -> fallback metadata, deterministic acceptance unchanged
# ---------------------------------------------------------------------------


def test_ai_failure_falls_back_and_acceptance_unchanged(failing_engine):
    record = {
        "company_name": "Acme Roofing LLC",
        "_discovery_source": "official_website",
    }
    gate = _verified_gate(record)
    assert gate.accepted is True

    intelligence = failing_engine.intelligence_for(
        record=record, gate=gate, query=QUERY
    )

    assert "ai" in intelligence and "qualification" in intelligence
    ai = intelligence["ai"]
    assert ai["ai_used"] is False
    assert ai["error"]
    assert ai["ai_score"] is None
    assert ai["score"] == ai["deterministic_score"]  # deterministic fallback
    assert intelligence["qualification"]["ai_used"] is False
    # Deterministic acceptance is unchanged by AI failure.
    assert gate.accepted is True
    assert gate.verification_status == VerificationStatus.verified


# ---------------------------------------------------------------------------
# 6. Namespace isolation — AI data never collides with verification
# ---------------------------------------------------------------------------


def test_namespace_isolation_additive(engine):
    record = {
        "company_name": "Acme Roofing LLC",
        "_discovery_source": "official_website",
    }
    gate = _verified_gate(record)
    intelligence = engine.intelligence_for(record=record, gate=gate, query=QUERY)

    # AI data lives ONLY under ai/qualification.
    assert set(intelligence) == {"ai", "qualification"}
    verification_authority = {
        "accepted",
        "verification_status",
        "verification_confidence",
        "hard_rejected",
        "source_tier",
        "business_type",
        "city",
        "state",
        "location_match",
        "field_evidence",
        "identity",
        "industry",
        "location",
    }
    assert not (set(intelligence["ai"]) & verification_authority)
    assert not (set(intelligence["qualification"]) & verification_authority)

    # Merging additively keeps every verification key from the gate intact.
    merged = {**gate.to_dict(), **intelligence}
    assert merged["accepted"] is True
    assert merged["verification_status"] == "verified"
    assert merged["verification_confidence"] > 0.0
    assert merged["ai"]["qualified"] is True


# ---------------------------------------------------------------------------
# 7. JSON serialization preserves verification status + evidence
# ---------------------------------------------------------------------------


def test_json_serialization_preserves_verification_and_evidence(engine):
    record = {
        "company_name": "Acme Roofing LLC",
        "_discovery_source": "official_website",
    }
    gate = _verified_gate(record)
    intelligence = engine.intelligence_for(record=record, gate=gate, query=QUERY)
    merged = {**gate.to_dict(), **intelligence}

    loaded = json.loads(json.dumps(merged))

    assert loaded["verification_status"] == "verified"
    assert loaded["accepted"] is True
    assert loaded["field_evidence"] == merged["field_evidence"]
    assert any(e["source"] == "licensing_board" for e in loaded["field_evidence"])
    assert loaded["ai"]["qualified"] is True
    assert loaded["qualification"]["qualified"] is True
    # AI keys never appear inside the verification namespace of the blob.
    assert "field_evidence" not in loaded["ai"]
    assert "verification_status" not in loaded["ai"]
