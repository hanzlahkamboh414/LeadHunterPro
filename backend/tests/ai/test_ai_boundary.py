"""Phase 2 — AI-boundary integration verification (fully offline).

Proves that the AI scoring path can NEVER upgrade a deterministically
rejected or unverified company into an accepted/verified lead, and that AI
may ONLY score / summarize an already-accepted lead (Phase 2 spec, H).

The boundary is enforced by construction — no second verification system:

- ``CompanyScorer.qualify`` / ``AIEngine.qualify_lead`` return ONLY scoring
  metadata (``score``, ``deterministic_score``, ``ai_score``, ``qualified``,
  ``qualification``, ``reasons``, ``strengths``, ``concerns``, ``ai_used``,
  ``error``). The parser reads exactly six AI keys, so a hostile AI response
  that also carries ``verification_status`` / ``business_type`` / ``city`` /
  ``field_evidence`` / ``decision_maker`` / ``project_signal`` is FILTERED —
  those keys never appear in the AI result.
- ``AcceptanceGate.evaluate`` is the sole acceptance authority; the connector's
  Step-4 loop gates on ``gate.accepted``/``gate.hard_rejected`` only, and
  attaches the gate verdict + AI scoring in SEPARATE metadata namespaces
  (``verification`` vs ``ai``).
- AI failure falls back to the deterministic score and never blocks a
  deterministic acceptance nor rescues a deterministic rejection.

Every test composes the two production surfaces the way a downstream consumer
does — gate verdict in ``verification`` metadata, AI scoring in ``ai``
metadata — then asserts the acceptance verdict still comes from the gate and
AI never collides with the verification namespace.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.ai.prompts.lead_score import build_lead_qualification_prompt
from app.engines.ai_engine import AIEngine
from app.engines.verification.acceptance_gate import AcceptanceGate
from app.engines.verification.identity_verifier import IdentityVerificationResult
from app.engines.verification.industry_verifier import IndustryVerificationResult
from app.engines.verification.location_verifier import LocationVerificationResult
from app.engines.verification.models import FieldEvidence, VerificationStatus

QUERY: dict = {"industry": "Roofing", "location": "Dallas Texas"}

# ---------------------------------------------------------------------------
# Fake AI provider payloads (offline, deterministic)
# ---------------------------------------------------------------------------

#: A benign, maximal AI verdict: highly qualified + high fit score.
QUALIFIED_RESPONSE = (
    '{"score": 95, "qualified": true, "qualification": "strong fit",'
    ' "reasons": ["roofing relevance"], "strengths": ["contractor"],'
    ' "concerns": []}'
)

#: Hostile AI that tries to manufacture verification evidence and a verdict.
HOSTILE_EVIDENCE_RESPONSE = (
    '{"score": 90, "qualified": true, "qualification": "fit",'
    ' "reasons": [], "strengths": [], "concerns": [],'
    ' "business_type": "contractor",'
    ' "field_evidence": [{"field": "city", "value": "Houston",'
    ' "source": "ai", "is_reliable": true}]}'
)

#: Hostile AI that tries to invent a decision maker from a bare name.
HOSTILE_DECISION_MAKER_RESPONSE = (
    '{"score": 90, "qualified": true, "qualification": "fit",'
    ' "reasons": [], "strengths": [], "concerns": [],'
    ' "decision_maker": "John Smith", "decision_makers": ["John Smith"]}'
)

#: Hostile AI that tries to invent a project/buying signal.
HOSTILE_PROJECT_RESPONSE = (
    '{"score": 90, "qualified": true, "qualification": "fit",'
    ' "reasons": [], "strengths": [], "concerns": [],'
    ' "project_signal": true, "project_references": ["Big Corp Tower 2025"]}'
)

#: Hostile AI that tries to claim a verified location.
HOSTILE_LOCATION_RESPONSE = (
    '{"score": 90, "qualified": true, "qualification": "fit",'
    ' "reasons": [], "strengths": [], "concerns": [],'
    ' "city": "Houston", "state": "TX"}'
)

#: AI that explicitly says the lead is NOT qualified.
UNQUALIFIED_RESPONSE = (
    '{"score": 60, "qualified": false, "qualification": "uncertain fit",'
    ' "reasons": [], "strengths": [], "concerns": []}'
)


def _ai_result(record: dict, payload: str = QUALIFIED_RESPONSE) -> dict:
    """Run the production AI scoring surface with a fake gateway (offline)."""
    with (
        patch("app.ai.scorer.AIGateway") as scorer_gw,
        patch("app.ai.summarizer.AIGateway") as _summarizer_gw,
    ):
        scorer_gw.return_value.ask.return_value = payload
        engine = AIEngine()
        return engine.qualify_lead(record, QUERY)


def _ai_failure(record: dict) -> dict:
    """AI scoring that raises — must fall back, never block or rescue."""
    with (
        patch("app.ai.scorer.AIGateway") as scorer_gw,
        patch("app.ai.summarizer.AIGateway") as _summarizer_gw,
    ):
        scorer_gw.return_value.ask.side_effect = RuntimeError(
            "omniroute unavailable"
        )
        engine = AIEngine()
        return engine.qualify_lead(record, QUERY)


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


def _gate(
    record: dict,
    *,
    identity=None,
    industry=None,
    location=None,
):
    """Run the deterministic AcceptanceGate on *record*."""
    return AcceptanceGate().evaluate(
        record=record,
        identity=identity if identity is not None else _identity(),
        industry=industry if industry is not None else _industry(),
        location=location if location is not None else _location(),
    )


def _verified_gate(record: dict):
    """A fully verified acceptance: contractor + official site + location."""
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


def _final_record(company: dict, gate, ai_result: dict) -> dict:
    """Downstream merge (connector idiom): verification + AI are SEPARATE namespaces.

    Mirrors how the TexasProcurementConnector attaches gate verdict as additive
    metadata — the acceptance authority stays in ``verification`` and AI scoring
    lives in its own ``ai`` namespace, never overwriting it.
    """
    merged = dict(company)
    merged["verification"] = gate.to_dict()
    merged["ai"] = ai_result
    return merged


# ---------------------------------------------------------------------------
# 1-3. Hard-rejected business types stay rejected even if AI says qualified
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("business_type", "name", "description"),
    [
        (
            "manufacturer",
            "Acme Shingle Manufacturer LLC",
            "roofing shingle manufacturer",
        ),
        (
            "supplier",
            "Acme Roofing Supplier LLC",
            "roofing materials supplier",
        ),
        (
            "association",
            "Texas Roofing Association",
            "roofing trade association",
        ),
    ],
)
def test_hard_rejected_business_type_stays_rejected_despite_ai_qualified(
    business_type, name, description
):
    record = {
        "company_name": name,
        "description": description,
        "website": "",
        "_discovery_source": "directory_crawl",
    }
    # Even a fully verified location cannot save a manufacturer/supplier/association.
    gate = _gate(
        record,
        industry=_industry(
            business_type=business_type,
            status="rejected",
            confidence=0.0,
            reasons=[f"{business_type} is not a construction contractor"],
        ),
        location=_location(
            city="Dallas", state="TX", status="verified", match=True, confidence=0.9
        ),
    )
    assert gate.accepted is False
    assert gate.hard_rejected is True
    assert gate.verification_status == VerificationStatus.rejected

    # AI screams qualified=True with a near-perfect fit score.
    ai = _ai_result(record)
    assert ai["qualified"] is True
    assert ai["ai_used"] is True

    final = _final_record(record, gate, ai)
    assert final["verification"]["accepted"] is False
    assert final["verification"]["verification_status"] == "rejected"
    # AI scoring metadata is present but lives in its own namespace.
    assert final["ai"]["qualified"] is True


# ---------------------------------------------------------------------------
# 4. Invalid / placeholder website rejection cannot be overridden by AI
# ---------------------------------------------------------------------------


def test_invalid_website_rejection_not_overridden_by_ai():
    record = {
        "company_name": "Acme Roofing LLC",
        "website": "http://example.com",
        "_discovery_source": "directory_crawl",
    }
    gate = _gate(
        record,
        identity=_identity(
            website_status="rejected",
            reasons=["placeholder/reserved URL: example.com"],
        ),
        location=_location(
            city="Dallas", state="TX", status="verified", match=True, confidence=0.9
        ),
    )
    assert gate.hard_rejected is True
    assert gate.verification_status == VerificationStatus.rejected

    ai = _ai_result(record)
    assert ai["qualified"] is True

    final = _final_record(record, gate, ai)
    assert final["verification"]["accepted"] is False
    assert final["verification"]["verification_status"] == "rejected"


# ---------------------------------------------------------------------------
# 5. Missing / unknown location cannot be upgraded into verified by AI
# ---------------------------------------------------------------------------


def test_unknown_location_cannot_be_upgraded_by_ai():
    record = {
        "company_name": "Acme Roofing LLC",
        "description": "commercial roofing contractor",
        "website": "",
        "_discovery_source": "directory_crawl",
    }
    gate = _gate(record, industry=_industry(status="unknown"))
    assert gate.city is None
    assert gate.state is None

    # AI "claims" Houston TX — it must not become verified location.
    ai = _ai_result(record, HOSTILE_LOCATION_RESPONSE)
    assert ai["qualified"] is True
    # The hostile city/state keys are filtered out of the AI result entirely.
    assert "city" not in ai
    assert "state" not in ai

    final = _final_record(record, gate, ai)
    assert final["verification"]["city"] == ""
    assert final["verification"]["state"] == ""


# ---------------------------------------------------------------------------
# 6. AI cannot create FieldEvidence / verification verdicts
# ---------------------------------------------------------------------------


def test_ai_cannot_inject_field_evidence_or_business_type():
    record = {
        "company_name": "Acme Roofing LLC",
        "description": "commercial roofing contractor",
        "website": "",
        "_discovery_source": "directory_crawl",
    }
    ai = _ai_result(record, HOSTILE_EVIDENCE_RESPONSE)
    assert ai["ai_used"] is True  # valid score still applied
    assert "field_evidence" not in ai
    assert "business_type" not in ai
    assert "accepted" not in ai
    assert "verification_status" not in ai

    # The gate's own evidence is unaffected by AI — none of it is AI-sourced.
    gate = _gate(
        record,
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
                    is_reliable=True,
                )
            ],
        ),
    )
    gate_evidence = gate.to_dict()["field_evidence"]
    assert gate_evidence  # real evidence is present...
    assert all(e["source"] != "ai" for e in gate_evidence)  # ...and AI made none.


# ---------------------------------------------------------------------------
# 7-8. AI cannot create decision makers / project signals without evidence
# ---------------------------------------------------------------------------


def test_ai_cannot_create_decision_maker_from_bare_name():
    record = {
        "company_name": "Acme Roofing LLC",
        "website": "",
        "_discovery_source": "directory_crawl",
    }
    ai = _ai_result(record, HOSTILE_DECISION_MAKER_RESPONSE)
    assert "decision_maker" not in ai
    assert "decision_makers" not in ai

    # The prompt itself only evaluates supplied evidence and forbids invention.
    prompt = build_lead_qualification_prompt(record, QUERY)
    assert "NEVER invent" in prompt
    assert "SUPPLIED COMPANY EVIDENCE" in prompt
    assert "John Smith" not in prompt  # the bare name was never supplied


def test_ai_cannot_create_project_signal_without_evidence():
    record = {
        "company_name": "Acme Roofing LLC",
        "website": "",
        "_discovery_source": "directory_crawl",
    }
    ai = _ai_result(record, HOSTILE_PROJECT_RESPONSE)
    assert "project_signal" not in ai
    assert "project_references" not in ai


# ---------------------------------------------------------------------------
# 9-11. AI may only score; the verdict always comes from the gate
# ---------------------------------------------------------------------------


def test_accepted_record_can_still_be_ai_scored():
    record = {
        "company_name": "Acme Roofing LLC",
        "_discovery_source": "official_website",
    }
    gate = _verified_gate(record)
    assert gate.accepted is True
    assert gate.verification_status == VerificationStatus.verified

    ai = _ai_result(record)
    assert ai["ai_used"] is True
    assert ai["ai_score"] == 95
    assert ai["qualified"] is True
    assert 0 <= ai["score"] <= 100

    final = _final_record(record, gate, ai)
    assert final["verification"]["accepted"] is True
    assert final["verification"]["verification_status"] == "verified"
    assert final["ai"]["score"] is not None


def test_ai_failure_does_not_bypass_deterministic_acceptance():
    # Accepted record: AI failure must not drop / block it.
    record = {
        "company_name": "Acme Roofing LLC",
        "_discovery_source": "official_website",
    }
    gate = _verified_gate(record)
    ai = _ai_failure(record)
    assert ai["ai_used"] is False
    assert ai["error"]
    assert ai["ai_score"] is None
    assert ai["score"] == ai["deterministic_score"]

    final = _final_record(record, gate, ai)
    assert final["verification"]["accepted"] is True
    assert final["verification"]["verification_status"] == "verified"

    # Rejected record: AI failure must not rescue it either.
    bad = {
        "company_name": "Acme Roofing Supplier LLC",
        "description": "roofing materials supplier",
        "website": "",
        "_discovery_source": "directory_crawl",
    }
    bad_gate = _gate(
        bad,
        industry=_industry(
            business_type="supplier",
            status="rejected",
            confidence=0.0,
            reasons=["supplier is not a construction contractor"],
        ),
        location=_location(
            city="Dallas", state="TX", status="verified", match=True, confidence=0.9
        ),
    )
    assert bad_gate.accepted is False
    final2 = _final_record(bad, bad_gate, _ai_failure(bad))
    assert final2["verification"]["accepted"] is False
    assert final2["verification"]["verification_status"] == "rejected"


def test_final_verdict_comes_from_gate_not_ai_qualified_boolean():
    # qualified=True never upgrades a rejection...
    rej = {
        "company_name": "Acme Shingle Manufacturer LLC",
        "description": "roofing shingle manufacturer",
        "website": "",
        "_discovery_source": "directory_crawl",
    }
    rej_gate = _gate(
        rej,
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
    rej_ai = _ai_result(rej)
    assert rej_ai["qualified"] is True
    assert _final_record(rej, rej_gate, rej_ai)["verification"]["accepted"] is False

    # ...and qualified=False never demotes an acceptance.
    acc = {
        "company_name": "Acme Roofing LLC",
        "_discovery_source": "official_website",
    }
    acc_gate = _verified_gate(acc)
    acc_ai = _ai_result(acc, UNQUALIFIED_RESPONSE)
    assert acc_ai["qualified"] is False
    final = _final_record(acc, acc_gate, acc_ai)
    assert final["verification"]["accepted"] is True
    assert final["verification"]["verification_status"] == "verified"


# ---------------------------------------------------------------------------
# 12. Serialization preserves the deterministic status + evidence
# ---------------------------------------------------------------------------


def test_serialization_preserves_deterministic_status_and_evidence():
    record = {
        "company_name": "Acme Roofing LLC",
        "_discovery_source": "official_website",
    }
    gate = _verified_gate(record)
    ai = _ai_result(record)
    final = _final_record(record, gate, ai)

    blob = json.dumps(final)
    loaded = json.loads(blob)

    verification = loaded["verification"]
    assert verification["accepted"] is True
    assert verification["verification_status"] == "verified"
    assert verification["verification_confidence"] > 0.0
    assert verification["field_evidence"] == final["verification"]["field_evidence"]
    assert any(
        e["source"] == "licensing_board" for e in verification["field_evidence"]
    )
    # AI keys survive in their own namespace — never inside verification.
    assert loaded["ai"]["qualified"] is True
    assert "accepted" not in loaded["ai"]
    assert "verification_status" not in loaded["ai"]
    assert "field_evidence" not in loaded["ai"]


# ---------------------------------------------------------------------------
# Structural guard: AI output namespace never collides with verification
# ---------------------------------------------------------------------------


def test_ai_output_namespace_never_collides_with_verification():
    record = {
        "company_name": "Acme Roofing LLC",
        "_discovery_source": "directory_crawl",
    }
    ai = _ai_result(record, HOSTILE_EVIDENCE_RESPONSE)

    # Keys the deterministic verdict owns; AI must never emit any of them.
    authoritative = {
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
        "evidence",
        "normalized_website",
    }
    assert not (set(ai) & authoritative), (
        "AI scoring output must never collide with the verification namespace"
    )
