"""IntentTimingAnalyzer — deterministic tests with injected AI."""

from __future__ import annotations

import json

from app.lead_research.intent_timing import IntentTimingAnalyzer
from app.lead_research.models import AIEvidence, CompanyProfile, PersonFindings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _good_ai_intent_response():
    return {
        "needs_estimation": "yes",
        "signal": "Active bid activity on public projects",
        "reason": "Company regularly bids on construction projects, likely needs estimation support",
        "evidence": [
            {"claim": "Bid on Highway 290 project", "source_url": "https://txdot.gov/bids", "source_type": "directory", "confidence": "verified"},
            {"claim": "10-50 employees, typical mid-size GC", "source_url": "", "source_type": "inferred", "confidence": "unverified"},
        ],
        "timing_window": "now",
        "timing_reason": "Q1 bid season active, multiple RFPs open",
        "timing_events": [
            {"claim": "RFP deadline Feb 15 2026", "source_url": "https://txdot.gov/rfp/2026-01", "source_type": "directory", "confidence": "verified"},
        ],
    }


def _company(**kwargs):
    defaults = dict(name="Acme Construction", industry="General Contractor", location="Dallas, TX", website="https://acme.com")
    defaults.update(kwargs)
    return CompanyProfile(**defaults)


def _person(**kwargs):
    defaults = dict(name="John Smith", role="Estimator", role_relevance=True, bound=True)
    defaults.update(kwargs)
    return PersonFindings(**defaults)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_yes_intent():
    analyzer = IntentTimingAnalyzer(ai_ask=lambda p: json.dumps(_good_ai_intent_response()))
    company = _company()
    person = _person()
    intent, timing = analyzer.analyze("john@acme.com", company, person)

    assert intent.needs_estimation == "yes"
    assert intent.signal == "Active bid activity on public projects"
    assert len(intent.evidence) == 2
    assert intent.evidence[0].confidence == "verified"
    assert intent.evidence[1].confidence == "unverified"

    assert timing.window == "now"
    assert timing.reason == "Q1 bid season active, multiple RFPs open"
    assert len(timing.events) == 1


def test_happy_path_no_intent():
    response = {
        "needs_estimation": "no",
        "signal": "Residential only, no bidding",
        "reason": "Company does residential work, no commercial bidding",
        "evidence": [],
        "timing_window": "unknown",
        "timing_reason": "Not applicable — no estimation need",
        "timing_events": [],
    }
    analyzer = IntentTimingAnalyzer(ai_ask=lambda p: json.dumps(response))
    intent, timing = analyzer.analyze("x@res.com", _company(name="Res Home", industry="Residential"), _person(name="", bound=False))

    assert intent.needs_estimation == "no"
    assert timing.window == "unknown"


def test_happy_path_unknown():
    response = {
        "needs_estimation": "unknown",
        "signal": "",
        "reason": "Insufficient information to determine",
        "evidence": [],
        "timing_window": "unknown",
        "timing_reason": "",
        "timing_events": [],
    }
    analyzer = IntentTimingAnalyzer(ai_ask=lambda p: json.dumps(response))
    intent, timing = analyzer.analyze("x@yz.com", _company(name="Y Corp"), _person(name="", bound=False))

    assert intent.needs_estimation == "unknown"
    assert timing.window == "unknown"


# ---------------------------------------------------------------------------
# AI failure modes
# ---------------------------------------------------------------------------

def test_ai_raises_exception():
    def boom(p):
        raise RuntimeError("API down")

    analyzer = IntentTimingAnalyzer(ai_ask=boom)
    intent, timing = analyzer.analyze("x@acme.com", _company(), _person())

    assert intent.needs_estimation == "unknown"
    assert "failed" in intent.reason.lower()
    assert timing.window == "unknown"


def test_ai_returns_garbage():
    analyzer = IntentTimingAnalyzer(ai_ask=lambda p: "not json")
    intent, timing = analyzer.analyze("x@acme.com", _company(), _person())

    assert intent.needs_estimation == "unknown"
    assert "unparseable" in intent.reason.lower()
    assert timing.window == "unknown"


def test_ai_returns_empty_json():
    analyzer = IntentTimingAnalyzer(ai_ask=lambda p: "{}")
    intent, timing = analyzer.analyze("x@acme.com", _company(), _person())

    assert intent.needs_estimation == "unknown"
    assert timing.window == "unknown"


# ---------------------------------------------------------------------------
# Facts summary building
# ---------------------------------------------------------------------------

def test_facts_summary_includes_company_info():
    """Verify the facts summary includes company details."""
    captured = {}

    def capturing_ai(prompt):
        captured["prompt"] = prompt
        return json.dumps(_good_ai_intent_response())

    analyzer = IntentTimingAnalyzer(ai_ask=capturing_ai)
    company = _company(name="Texas Builders", industry="Commercial GC", location="Houston, TX")
    person = _person(name="Mike Jones", role="VP Estimating", role_relevance=True, bound=True)
    analyzer.analyze("mike@txbuilders.com", company, person)

    prompt = captured["prompt"]
    assert "Texas Builders" in prompt
    assert "Commercial GC" in prompt
    assert "Houston, TX" in prompt
    assert "Mike Jones" in prompt
    assert "VP Estimating" in prompt


def test_facts_summary_includes_verified_facts():
    """Verified company facts appear in the summary."""
    captured = {}

    def capturing_ai(prompt):
        captured["prompt"] = prompt
        return json.dumps(_good_ai_intent_response())

    analyzer = IntentTimingAnalyzer(ai_ask=capturing_ai)
    company = _company(facts=[
        AIEvidence(claim="Founded 2005", source_url="https://acme.com/about", confidence="verified"),
        AIEvidence(claim="$20M revenue", confidence="unverified"),
    ])
    analyzer.analyze("x@acme.com", company, _person())

    prompt = captured["prompt"]
    assert "Founded 2005" in prompt
    assert "$20M revenue" in prompt
    assert "[verified]" in prompt
    assert "[unverified]" in prompt


def test_facts_summary_minimal_info():
    """Minimal company/person info still produces valid summary."""
    captured = {}

    def capturing_ai(prompt):
        captured["prompt"] = prompt
        return json.dumps(_good_ai_intent_response())

    analyzer = IntentTimingAnalyzer(ai_ask=capturing_ai)
    analyzer.analyze("x@unknown.com", CompanyProfile(), PersonFindings())

    prompt = captured["prompt"]
    assert "no facts available" in prompt.lower() or "Company:" in prompt


def test_source_note_passes_through_intent_and_timing():
    """source_note from the AI response survives in intent evidence + timing
    events — the human-readable WHERE travels with every cited observation."""
    response = dict(_good_ai_intent_response())
    response["evidence"][0]["source_note"] = "TxDOT bid board"
    response["timing_events"][0]["source_note"] = "RFP listing"

    analyzer = IntentTimingAnalyzer(ai_ask=lambda p: json.dumps(response))
    intent, timing = analyzer.analyze("john@acme.com", _company(), _person())

    assert intent.evidence[0].source_note == "TxDOT bid board"
    assert timing.events[0].source_note == "RFP listing"


def test_bare_root_verified_intent_evidence_demoted():
    """Exact-page guard on the intent/timing path: a 'verified' observation
    citing only a bare domain root is demoted to 'unverified'."""
    response = {
        "needs_estimation": "yes",
        "signal": "Hiring estimators",
        "reason": "Careers page lists open estimator roles",
        "evidence": [
            {"claim": "Hiring estimators", "source_url": "https://acme.com", "source_type": "website", "confidence": "verified"},
        ],
        "timing_window": "soon",
        "timing_reason": "Hiring signals",
        "timing_events": [
            {"claim": "Opened roles", "source_url": "https://acme.com/careers", "source_type": "careers_page", "confidence": "verified"},
        ],
    }
    analyzer = IntentTimingAnalyzer(ai_ask=lambda p: json.dumps(response))
    intent, timing = analyzer.analyze("x@acme.com", _company(), _person())

    assert intent.evidence[0].confidence == "unverified"
    assert intent.evidence[0].source_note == "source location not reported"
    assert timing.events[0].confidence == "verified"  # exact /careers URL survives


def test_facts_summary_unbound_person():
    """Unbound person with evidence shows person details."""
    captured = {}

    def capturing_ai(prompt):
        captured["prompt"] = prompt
        return json.dumps(_good_ai_intent_response())

    analyzer = IntentTimingAnalyzer(ai_ask=capturing_ai)
    person = PersonFindings(
        name="",
        evidence=[AIEvidence(claim="Email found on contact page", source_url="https://acme.com/contact")],
    )
    analyzer.analyze("x@acme.com", _company(), person)

    prompt = captured["prompt"]
    assert "not conclusively identified" in prompt.lower()
    assert "Email found on contact page" in prompt
