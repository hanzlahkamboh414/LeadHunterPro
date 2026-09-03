"""LeadScorer — deterministic tests with injected AI."""

from __future__ import annotations

import json

from app.lead_research.models import AIEvidence, CompanyProfile, IntentAssessment, PersonFindings, TimingAssessment
from app.lead_research.scoring import LeadScorer


def _company(**kw):
    return CompanyProfile(name=kw.get("name", "Acme"), industry=kw.get("industry", "GC"), location=kw.get("location", "TX"))


def _person(**kw):
    return PersonFindings(
        name=kw.get("name", "John"),
        role=kw.get("role", "Estimator"),
        role_relevance=kw.get("role_relevance", True),
        bound=kw.get("bound", True),
    )


def _intent(**kw):
    return IntentAssessment(needs_estimation=kw.get("needs_estimation", "yes"), signal=kw.get("signal", "bids"))


def _timing(**kw):
    return TimingAssessment(window=kw.get("window", "now"))


# --- Happy path ---

def test_high_score_contact_now():
    resp = {"fit": "Strong", "potential_score": 8.0, "recommendation": "contact_now", "reasoning": "Good fit"}
    scorer = LeadScorer(ai_ask=lambda p: json.dumps(resp))
    fit, score, rec = scorer.score("x@acme.com", _company(), _person(bound=True), _intent(), _timing())
    assert score == 8.0
    assert rec == "contact_now"


def test_medium_score_nurture():
    resp = {"fit": "Moderate", "potential_score": 5.0, "recommendation": "nurture", "reasoning": "OK"}
    scorer = LeadScorer(ai_ask=lambda p: json.dumps(resp))
    fit, score, rec = scorer.score("x@acme.com", _company(), _person(bound=True), _intent(), _timing())
    assert score == 5.0
    assert rec == "nurture"


def test_low_score_skip():
    resp = {"fit": "Weak", "potential_score": 1.5, "recommendation": "skip", "reasoning": "No fit"}
    scorer = LeadScorer(ai_ask=lambda p: json.dumps(resp))
    fit, score, rec = scorer.score("x@acme.com", _company(), _person(bound=False), _intent(), _timing())
    assert score == 1.5
    assert rec == "skip"


# --- Gate overrides ---

def test_gate_high_score_unbound_downgrades():
    """AI says contact_now but person not bound → nurture."""
    resp = {"fit": "Good", "potential_score": 7.0, "recommendation": "contact_now", "reasoning": ""}
    scorer = LeadScorer(ai_ask=lambda p: json.dumps(resp))
    _, _, rec = scorer.score("x@acme.com", _company(), _person(bound=False), _intent(), _timing())
    assert rec == "nurture"  # gate override: score >= 3 but not bound


def test_gate_medium_score_unbound_stays_nurture():
    resp = {"fit": "OK", "potential_score": 4.0, "recommendation": "nurture", "reasoning": ""}
    scorer = LeadScorer(ai_ask=lambda p: json.dumps(resp))
    _, _, rec = scorer.score("x@acme.com", _company(), _person(bound=False), _intent(), _timing())
    assert rec == "nurture"


def test_gate_very_low_skip():
    resp = {"fit": "Bad", "potential_score": 0.5, "recommendation": "nurture", "reasoning": ""}
    scorer = LeadScorer(ai_ask=lambda p: json.dumps(resp))
    _, _, rec = scorer.score("x@acme.com", _company(), _person(bound=False), _intent(), _timing())
    assert rec == "skip"


def test_gate_exact_6_bound_is_contact_now():
    resp = {"fit": "Good", "potential_score": 6.0, "recommendation": "nurture", "reasoning": ""}
    scorer = LeadScorer(ai_ask=lambda p: json.dumps(resp))
    _, _, rec = scorer.score("x@acme.com", _company(), _person(bound=True), _intent(), _timing())
    assert rec == "contact_now"


def test_gate_exact_3_unbound_is_nurture():
    resp = {"fit": "Fair", "potential_score": 3.0, "recommendation": "skip", "reasoning": ""}
    scorer = LeadScorer(ai_ask=lambda p: json.dumps(resp))
    _, _, rec = scorer.score("x@acme.com", _company(), _person(bound=False), _intent(), _timing())
    assert rec == "nurture"


# --- AI failure modes ---

def test_ai_raises_exception_default_score():
    scorer = LeadScorer(ai_ask=lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    _, score, rec = scorer.score("x@acme.com", _company(), _person(bound=True, name="John"), _intent(), _timing())
    assert rec == "nurture"
    assert score == 3.0


def test_ai_garbage_response_default_score():
    scorer = LeadScorer(ai_ask=lambda p: "not json")
    _, score, rec = scorer.score("x@acme.com", _company(), _person(bound=False), _intent(), _timing())
    assert rec == "skip"
    assert score == 0.0


def test_ai_empty_json_default_score():
    scorer = LeadScorer(ai_ask=lambda p: "{}")
    _, score, rec = scorer.score("x@acme.com", _company(), _person(bound=False), _intent(), _timing())
    assert rec == "skip"
