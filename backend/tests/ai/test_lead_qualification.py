"""Tests for AI lead qualification (CompanyScorer.qualify) and AIEngine."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.ai.prompts.lead_score import build_lead_qualification_prompt
from app.ai.scorer import CompanyScorer
from app.engines.ai_engine import AIEngine

DATA: dict = {
    "title": "Dallas Roofing Co",
    "company_name": "Dallas Roofing Co",
    "website": "https://dallasroofing.example",
    "city": "Dallas",
    "state": "TX",
    "description": "Commercial roofing contractor serving Dallas, Texas",
    "emails": [],
    "phones": [],
}
QUERY: dict = {"industry": "Roofing", "location": "Dallas Texas"}

# title(20) + description(10) for DATA above.
DETERMINISTIC_SCORE = 30


@pytest.fixture
def scorer() -> CompanyScorer:
    """AI-enabled CompanyScorer with a fake gateway (no real AI client)."""
    with patch("app.ai.scorer.AIGateway") as mock_gw_class:
        s = CompanyScorer(use_ai=True)
        s._gateway = mock_gw_class.return_value
        yield s


def _valid_response(score: int) -> str:
    return (
        f'{{"score": {score}, "qualified": true, "qualification": "strong fit", '
        f'"reasons": ["industry relevance"], "strengths": ["local"], '
        f'"concerns": []}}'
    )


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------


def test_deterministic_fallback_when_ai_raises(scorer: CompanyScorer) -> None:
    scorer._gateway.ask.side_effect = RuntimeError("connection refused")

    result = scorer.qualify(DATA, QUERY)

    assert result["ai_used"] is False
    assert result["ai_score"] is None
    assert result["score"] == result["deterministic_score"] == DETERMINISTIC_SCORE
    assert "connection refused" in result["error"]
    assert result["qualified"] is None


def test_deterministic_fallback_when_ai_returns_malformed_json(
    scorer: CompanyScorer,
) -> None:
    scorer._gateway.ask.return_value = "this is not json at all"

    result = scorer.qualify(DATA, QUERY)

    assert result["ai_used"] is False
    assert result["ai_score"] is None
    assert result["score"] == result["deterministic_score"] == DETERMINISTIC_SCORE
    assert result["error"]


def test_ai_unused_when_flag_disabled() -> None:
    s = CompanyScorer(use_ai=False)

    result = s.qualify(DATA, QUERY)

    assert result["ai_used"] is False
    assert result["ai_score"] is None
    assert result["score"] == DETERMINISTIC_SCORE


# ---------------------------------------------------------------------------
# Valid AI response + blending
# ---------------------------------------------------------------------------


def test_valid_ai_response_blends_scores(scorer: CompanyScorer) -> None:
    scorer._gateway.ask.return_value = _valid_response(80)

    result = scorer.qualify(DATA, QUERY)

    assert result["ai_used"] is True
    assert result["ai_score"] == 80
    assert result["deterministic_score"] == DETERMINISTIC_SCORE
    assert result["score"] == round(0.5 * DETERMINISTIC_SCORE + 0.5 * 80)  # 55
    assert result["qualified"] is True
    assert result["qualification"] == "strong fit"
    assert result["reasons"] == ["industry relevance"]
    assert result["strengths"] == ["local"]
    assert result["concerns"] == []
    assert result["error"] is None


def test_ai_response_tolerates_code_fences(scorer: CompanyScorer) -> None:
    scorer._gateway.ask.return_value = (
        '```json\n{"score": 90, "qualified": true, "qualification": "excellent"}\n```'
    )

    result = scorer.qualify(DATA, QUERY)

    assert result["ai_used"] is True
    assert result["ai_score"] == 90
    assert result["score"] == round(0.5 * DETERMINISTIC_SCORE + 0.5 * 90)


# ---------------------------------------------------------------------------
# Score clamping / invalid scores
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(150, 100), (-5, 0), (0, 0), (100, 100), (99.6, 100), ("42", 42)],
)
def test_ai_score_is_clamped(scorer: CompanyScorer, raw: object, expected: int) -> None:
    scorer._gateway.ask.return_value = f'{{"score": {raw}, "qualified": true}}'

    result = scorer.qualify(DATA, QUERY)

    assert result["ai_score"] == expected
    assert 0 <= result["score"] <= 100


@pytest.mark.parametrize(
    "bad_score", ['"abc"', "null", "[]", "true", "false", "{}"]
)
def test_non_numeric_ai_score_falls_back(scorer: CompanyScorer, bad_score: str) -> None:
    scorer._gateway.ask.return_value = f'{{"score": {bad_score}, "qualified": true}}'

    result = scorer.qualify(DATA, QUERY)

    assert result["ai_used"] is False
    assert result["ai_score"] is None
    assert result["score"] == result["deterministic_score"] == DETERMINISTIC_SCORE
    assert result["error"]


# ---------------------------------------------------------------------------
# Prompt content
# ---------------------------------------------------------------------------


def test_query_industry_and_location_included_in_prompt() -> None:
    prompt = build_lead_qualification_prompt(DATA, QUERY)

    assert "Roofing" in prompt
    assert "Dallas Texas" in prompt


def test_company_evidence_included_in_prompt() -> None:
    prompt = build_lead_qualification_prompt(DATA, QUERY)

    assert "Dallas Roofing Co" in prompt
    assert "Commercial roofing contractor" in prompt


def test_prompt_prohibits_inventing_evidence() -> None:
    prompt = build_lead_qualification_prompt(DATA, QUERY)

    assert "NEVER invent" in prompt
    assert "evaluate ONLY" in prompt


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


def test_output_contract(scorer: CompanyScorer) -> None:
    scorer._gateway.ask.return_value = _valid_response(70)

    result = scorer.qualify(DATA, QUERY)

    for key in (
        "score",
        "deterministic_score",
        "ai_score",
        "qualified",
        "qualification",
        "reasons",
        "strengths",
        "concerns",
        "ai_used",
        "error",
    ):
        assert key in result, f"missing key {key!r}"
    assert isinstance(result["score"], int)
    assert isinstance(result["ai_score"], int)
    assert isinstance(result["qualified"], bool)
    assert isinstance(result["reasons"], list)
    assert isinstance(result["ai_used"], bool)


# ---------------------------------------------------------------------------
# AIEngine integration
# ---------------------------------------------------------------------------


def test_score_company_returns_int_0_100_when_ai_unavailable() -> None:
    with (
        patch("app.ai.scorer.AIGateway") as scorer_gw,
        patch("app.ai.summarizer.AIGateway") as _summarizer_gw,
    ):
        scorer_gw.return_value.ask.side_effect = RuntimeError("omniroute unavailable")
        engine = AIEngine()

        score = engine.score_company(DATA)

    assert isinstance(score, int)
    assert 0 <= score <= 100
    assert score == DETERMINISTIC_SCORE


def test_aiengine_qualify_lead_returns_contract_when_ai_works() -> None:
    with (
        patch("app.ai.scorer.AIGateway") as scorer_gw,
        patch("app.ai.summarizer.AIGateway") as _summarizer_gw,
    ):
        scorer_gw.return_value.ask.return_value = _valid_response(60)
        engine = AIEngine()

        result = engine.qualify_lead(DATA, QUERY)

    assert result["ai_used"] is True
    assert result["ai_score"] == 60
    assert result["score"] == round(0.5 * DETERMINISTIC_SCORE + 0.5 * 60)  # 45
    assert result["qualified"] is True
