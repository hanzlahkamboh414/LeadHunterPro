"""AILeadResearchAgent — pipeline orchestration tests."""

from __future__ import annotations

import json

from app.lead_research.agent import AILeadResearchAgent, _is_free_mail, _is_generic_local_part
from app.lead_research.company_research import CompanyResearcher
from app.lead_research.intent_timing import IntentTimingAnalyzer
from app.lead_research.models import CompanyProfile, PersonFindings
from app.lead_research.person_research_ai import PersonResearcherAI
from app.lead_research.scoring import LeadScorer
from tests.lead_research.conftest import make_fake_ai, make_fake_fetch, make_fake_refine, make_fake_search


# ---------------------------------------------------------------------------
# Triage helpers
# ---------------------------------------------------------------------------

def test_is_free_mail():
    assert _is_free_mail("gmail.com") is True
    assert _is_free_mail("yahoo.com") is True
    assert _is_free_mail("acme.com") is False


def test_is_generic_local_part():
    assert _is_generic_local_part("info@acme.com") is True
    assert _is_generic_local_part("office@acme.com") is True
    assert _is_generic_local_part("john@acme.com") is False


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def _make_agent(ai_response=None):
    """Build an agent with all fake seams."""
    if ai_response is None:
        ai_response = {
            "company_name": "Acme Construction",
            "industry": "General Contractor",
            "location": "Dallas, TX",
            "website": "https://acme.com",
            "facts": [
                {"claim": "Estimating services needed", "source_url": "https://acme.com", "source_type": "website", "confidence": "verified"},
            ],
        }

    company = CompanyResearcher(
        ai_ask=make_fake_ai(ai_response),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "Jane Doe",
            "person_role": "Estimator",
            "role_relevance": True,
            "bound": True,
            "evidence": [{"claim": "Team page", "source_url": "https://acme.com/team", "source_type": "website", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "yes",
        "signal": "Active bids",
        "reason": "Regularly bids",
        "evidence": [{"claim": "Bid activity", "source_url": "https://acme.com/bids", "source_type": "website", "confidence": "verified"}],
        "timing_window": "now",
        "timing_reason": "Q1 season",
        "timing_events": [{"claim": "RFP open", "source_url": "", "source_type": "inferred", "confidence": "unverified"}],
    }))
    scorer = LeadScorer(ai_ask=make_fake_ai({
        "fit": "Strong fit — active GC",
        "potential_score": 7.5,
        "recommendation": "contact_now",
        "reasoning": "Good match",
    }))

    return AILeadResearchAgent(
        company_researcher=company,
        person_researcher=person,
        intent_analyzer=intent,
        scorer=scorer,
    )


def test_full_pipeline_happy_path():
    agent = _make_agent()
    dossier = agent.research("jane@acme.com", "acme.com")

    assert dossier.email == "jane@acme.com"
    assert dossier.refined_domain == "acme.com"
    assert dossier.company.name == "Acme Construction"
    assert dossier.person.name == "Jane Doe"
    assert dossier.person.bound is True
    assert dossier.intent.needs_estimation == "yes"
    assert dossier.timing.window == "now"
    assert dossier.potential_score == 7.5
    assert dossier.recommendation == "contact_now"
    assert "company_research" in dossier.sources_checked
    assert "person_research" in dossier.sources_checked
    assert "intent_timing" in dossier.sources_checked
    assert "scoring" in dossier.sources_checked
    assert dossier.source_errors == {}


def test_triage_free_mail_skips_all():
    agent = _make_agent()
    dossier = agent.research("john@gmail.com", "gmail.com")
    assert dossier.recommendation == "skip"
    assert "free mail" in dossier.fit.lower()
    assert dossier.sources_checked == []


def test_triage_generic_email_skips_all():
    agent = _make_agent()
    dossier = agent.research("info@acme.com", "acme.com")
    assert dossier.recommendation == "skip"
    assert "generic" in dossier.fit.lower()
    assert dossier.sources_checked == []


def test_company_ai_failure_still_runs_pipeline():
    """Stage 1 AI failure → graceful degradation, pipeline continues."""
    company = CompanyResearcher(
        ai_ask=lambda p: (_ for _ in ()).throw(RuntimeError("AI down")),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "Jane", "person_role": "PM", "role_relevance": True, "bound": True,
            "evidence": [{"claim": "Found", "source_url": "https://acme.com", "source_type": "website", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "yes", "signal": "", "reason": "", "evidence": [],
        "timing_window": "unknown", "timing_reason": "", "timing_events": [],
    }))
    scorer = LeadScorer(ai_ask=make_fake_ai({
        "fit": "partial", "potential_score": 4.0, "recommendation": "nurture", "reasoning": "",
    }))

    agent = AILeadResearchAgent(company_researcher=company, person_researcher=person, intent_analyzer=intent, scorer=scorer)
    dossier = agent.research("jane@acme.com", "acme.com")

    # Company AI failed gracefully → partial company data, no exception
    assert dossier.company.name == ""
    assert "AI call failed" in dossier.company.facts[0].claim
    # But other stages still ran
    assert "person_research" in dossier.sources_checked
    assert "intent_timing" in dossier.sources_checked
    assert "scoring" in dossier.sources_checked


def test_person_ai_failure_still_scores():
    """Stage 2 AI failure → graceful degradation, scoring still runs."""
    agent = _make_agent()
    # Override person to fail
    agent._person = PersonResearcherAI(
        deterministic=None,
        ai_ask=lambda p: (_ for _ in ()).throw(RuntimeError("person AI down")),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    dossier = agent.research("jane@acme.com", "acme.com")
    # Person AI failed gracefully → partial person data
    assert dossier.person.name == ""
    assert "AI call failed" in dossier.person.evidence[0].claim
    # Scoring still ran
    assert "scoring" in dossier.sources_checked


def test_dossier_to_dict_roundtrip():
    """Dossier from agent can be serialized and deserialized."""
    agent = _make_agent()
    dossier = agent.research("jane@acme.com", "acme.com")
    d = dossier.to_dict()
    from app.lead_research.models import LeadDossier
    dossier2 = LeadDossier.from_dict(d)
    assert dossier2.email == dossier.email
    assert dossier2.company.name == dossier.company.name
    assert dossier2.potential_score == dossier.potential_score
