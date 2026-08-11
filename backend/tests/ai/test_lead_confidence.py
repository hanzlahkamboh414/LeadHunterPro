"""Offline tests for Increment6 — evidence-backed confidence + justification.

Pins the two new ``CompanyScorer.qualify`` output keys the Lead qualification
gate (rule #6) consumes: ``ai_confidence`` (0-100, must reach 90 to qualify)
and ``justification`` (written reasoning citing specific evidence URLs). Fully
offline — the AI gateway is a stub; every scorer test asserts the honest
fallback when the AI omits or fails the new fields (never a fabricated
confidence).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.ai.prompts.lead_score import build_lead_qualification_prompt
from app.ai.scorer import CompanyScorer

DATA: dict = {
    "title": "Dallas Roofing Co",
    "company_name": "Dallas Roofing Co",
    "website": "https://dallasroofing.example",
    "city": "Dallas",
    "state": "TX",
    "description": "Commercial roofing contractor serving Dallas, Texas",
    "emails": [],
    "phones": [],
    "intent_evidence": [
        {
            "type": "bid_award",
            "source_url": "https://www.usaspending.gov/award/ABC123",
            "snippet": "Award for roofing services",
            "date": "2026-06-01",
            "source": "usaspending",
        },
        {
            "type": "hiring",
            "source_url": "https://dallasroofing.example/careers",
            "snippet": "Now hiring experienced roofers",
            "date": "",
            "source": "company_site",
        },
    ],
}

QUERY: dict = {"industry": "Roofing", "location": "Dallas Texas"}

# title(20) + description(10) for DATA.
DETERMINISTIC_SCORE = 30


def _response(score: int = 80, **extra: object) -> str:
    """A valid AI payload; the new fields are optional (legacy responses lack them)."""
    payload: dict[str, object] = {
        "score": score,
        "qualified": True,
        "qualification": "strong fit",
        "reasons": ["bid award evidence"],
        "strengths": ["active hiring"],
        "concerns": [],
    }
    payload.update(extra)
    return json.dumps(payload)


@pytest.fixture
def scorer() -> CompanyScorer:
    """AI-enabled CompanyScorer with a fake gateway (no real AI client)."""
    with patch("app.ai.scorer.AIGateway") as mock_gw_class:
        s = CompanyScorer(use_ai=True)
        s._gateway = mock_gw_class.return_value
        yield s


class TestConfidenceJustification:

    def test_ai_confidence_and_justification_passed_through(self, scorer) -> None:
        scorer._gateway.ask.return_value = _response(
            confidence=95,
            justification="Award ABC123 proves an active buying window",
        )

        result = scorer.qualify(DATA, QUERY)

        assert result["ai_used"] is True
        assert result["ai_confidence"] == 95
        assert result["justification"] == "Award ABC123 proves an active buying window"

    def test_confidence_missing_falls_back_to_blended_score(self, scorer) -> None:
        scorer._gateway.ask.return_value = _response(score=80)  # legacy: no confidence

        result = scorer.qualify(DATA, QUERY)

        assert result["ai_confidence"] == round(0.5 * DETERMINISTIC_SCORE + 0.5 * 80)

    def test_justification_missing_falls_back_to_qualification(self, scorer) -> None:
        scorer._gateway.ask.return_value = _response(qualification="strong fit")

        result = scorer.qualify(DATA, QUERY)

        assert result["justification"] == "strong fit"

    def test_ai_failure_deterministic_confidence_empty_justification(
        self, scorer
    ) -> None:
        scorer._gateway.ask.side_effect = RuntimeError("down")

        result = scorer.qualify(DATA, QUERY)

        assert result["ai_used"] is False
        assert result["ai_confidence"] == DETERMINISTIC_SCORE
        assert result["justification"] == ""

    def test_ai_disabled_deterministic_confidence_empty_justification(self) -> None:
        result = CompanyScorer(use_ai=False).qualify(DATA, QUERY)

        assert result["ai_used"] is False
        assert result["ai_confidence"] == DETERMINISTIC_SCORE
        assert result["justification"] == ""

    def test_confidence_clamped_to_0_100(self, scorer) -> None:
        scorer._gateway.ask.return_value = _response(confidence=150)

        assert scorer.qualify(DATA, QUERY)["ai_confidence"] == 100

    def test_invalid_confidence_falls_back_to_blended_score(self, scorer) -> None:
        scorer._gateway.ask.return_value = _response(confidence="abc")

        result = scorer.qualify(DATA, QUERY)

        assert result["ai_confidence"] == result["score"]

    def test_explicit_zero_confidence_is_respected(self, scorer) -> None:
        scorer._gateway.ask.return_value = _response(confidence=0)

        assert scorer.qualify(DATA, QUERY)["ai_confidence"] == 0


class TestPrompt:

    def test_intent_evidence_rendered_with_urls(self) -> None:
        prompt = build_lead_qualification_prompt(DATA, QUERY)

        assert "BUYING-INTENT EVIDENCE" in prompt
        assert "https://www.usaspending.gov/award/ABC123" in prompt
        assert "https://dallasroofing.example/careers" in prompt
        assert "[bid_award]" in prompt
        assert "[hiring]" in prompt

    def test_untraceable_signal_is_not_rendered(self) -> None:
        company = {
            "intent_evidence": [
                {"type": "project", "source_url": "  ", "snippet": "phantom signal"}
            ]
        }

        prompt = build_lead_qualification_prompt(company, QUERY)

        assert "phantom signal" not in prompt
        assert "(no buying-intent evidence supplied)" in prompt

    def test_missing_intent_evidence_reported_not_invented(self) -> None:
        company = {"company_name": "Acme Roofing LLC", "description": "contractor"}

        prompt = build_lead_qualification_prompt(company, QUERY)

        assert "(no buying-intent evidence supplied)" in prompt

    def test_prompt_asks_for_confidence_and_justification(self) -> None:
        prompt = build_lead_qualification_prompt(DATA, QUERY)

        assert '"confidence": 0' in prompt
        assert '"justification"' in prompt
        assert "cite the specific evidence" in prompt

    def test_prompt_binds_confidence_to_evidence(self) -> None:
        prompt = build_lead_qualification_prompt(DATA, QUERY)

        assert "NEVER invent" in prompt
        assert "NO traceable" in prompt
        assert "confidence stays LOW" in prompt

    def test_existing_prompt_guards_survive(self) -> None:
        """The boundary tests pin these strings — they must not regress."""
        prompt = build_lead_qualification_prompt(DATA, QUERY)

        assert "NEVER invent" in prompt
        assert "SUPPLIED COMPANY EVIDENCE" in prompt
        assert "evaluate ONLY" in prompt


class TestOutputContract:

    def test_output_carries_new_keys(self, scorer) -> None:
        scorer._gateway.ask.return_value = _response(confidence=95, justification="x")

        result = scorer.qualify(DATA, QUERY)

        assert "ai_confidence" in result
        assert "justification" in result
        assert isinstance(result["ai_confidence"], int)
        assert isinstance(result["justification"], str)

    def test_new_keys_never_collide_with_verification(self, scorer) -> None:
        scorer._gateway.ask.return_value = _response(confidence=95, justification="x")

        result = scorer.qualify(DATA, QUERY)

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
        assert not (set(result) & authoritative)
