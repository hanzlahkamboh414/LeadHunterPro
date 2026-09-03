"""LeadResearchService — store + agent + persist tests."""

from __future__ import annotations

import json

from app.lead_research.agent import AILeadResearchAgent
from app.lead_research.company_research import CompanyResearcher
from app.lead_research.intent_timing import IntentTimingAnalyzer
from app.lead_research.models import LeadDossier
from app.lead_research.person_research_ai import PersonResearcherAI
from app.lead_research.scoring import LeadScorer
from app.lead_research.service import LeadResearchService, LeadResearchStore
from tests.lead_research.conftest import make_fake_ai, make_fake_fetch, make_fake_refine, make_fake_search


def _service(tmp_path):
    """Build a service with all fake seams and a temp DB."""
    company = CompanyResearcher(
        ai_ask=make_fake_ai({
            "company_name": "Test Co", "industry": "GC", "location": "TX", "website": "https://test.com",
            "facts": [{"claim": "Test fact", "source_url": "https://test.com", "source_type": "website", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("test.com"),
    )
    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "Bob", "person_role": "PM", "role_relevance": True, "bound": True,
            "evidence": [{"claim": "Team page", "source_url": "https://test.com/team", "source_type": "website", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "yes", "signal": "Active", "reason": "Bids", "evidence": [],
        "timing_window": "now", "timing_reason": "Q1", "timing_events": [],
    }))
    scorer = LeadScorer(ai_ask=make_fake_ai({
        "fit": "Good", "potential_score": 7.0, "recommendation": "contact_now", "reasoning": "",
    }))
    agent = AILeadResearchAgent(
        company_researcher=company, person_researcher=person,
        intent_analyzer=intent, scorer=scorer,
    )
    store = LeadResearchStore(str(tmp_path / "test.db"))
    return LeadResearchService(store=store, agent=agent)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def test_store_save_and_get(tmp_path):
    store = LeadResearchStore(str(tmp_path / "test.db"))
    dossier = LeadDossier(email="x@test.com", domain="test.com", potential_score=5.0)
    store.save(dossier)
    got = store.get("x@test.com")
    assert got is not None
    assert got.email == "x@test.com"
    assert got.potential_score == 5.0


def test_store_upsert(tmp_path):
    store = LeadResearchStore(str(tmp_path / "test.db"))
    store.save(LeadDossier(email="x@test.com", domain="test.com", potential_score=3.0))
    store.save(LeadDossier(email="x@test.com", domain="test.com", potential_score=7.0))
    assert store.count() == 1
    got = store.get("x@test.com")
    assert got.potential_score == 7.0


def test_store_get_missing_returns_none(tmp_path):
    store = LeadResearchStore(str(tmp_path / "test.db"))
    assert store.get("nobody@test.com") is None


def test_store_list_all(tmp_path):
    store = LeadResearchStore(str(tmp_path / "test.db"))
    store.save(LeadDossier(email="a@test.com", domain="test.com"))
    store.save(LeadDossier(email="b@test.com", domain="test.com"))
    assert store.count() == 2
    all_leads = store.list_all()
    assert len(all_leads) == 2


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

def test_service_research_persists(tmp_path):
    svc = _service(tmp_path)
    dossier = svc.research("bob@test.com", "test.com")
    assert dossier.potential_score == 7.0
    # Verify persisted
    got = svc.get("bob@test.com")
    assert got is not None
    assert got.potential_score == 7.0


def test_service_list_leads(tmp_path):
    svc = _service(tmp_path)
    svc.research("bob@test.com", "test.com")
    svc.research("alice@test.com", "test.com")
    leads = svc.list_leads()
    assert len(leads) == 2


def test_service_research_batch(tmp_path):
    svc = _service(tmp_path)
    records = [
        {"email": "bob@test.com", "domain": "test.com"},
        {"email": "alice@test.com", "domain": "test.com"},
        {"email": "", "domain": "test.com"},  # skipped
    ]
    results = svc.research_batch(records)
    assert len(results) == 2  # empty email skipped


def test_service_research_batch_with_failure(tmp_path):
    """Batch continues even if one record fails."""
    svc = _service(tmp_path)
    records = [
        {"email": "bob@test.com", "domain": "test.com"},
        {"email": "fail@test.com", "domain": "test.com"},
    ]
    # Override agent to fail on fail@test.com
    original_research = svc.agent.research

    def failing_research(email, domain):
        if email == "fail@test.com":
            raise RuntimeError("boom")
        return original_research(email, domain)

    svc.agent.research = failing_research
    results = svc.research_batch(records)
    assert len(results) == 1
    assert results[0].email == "bob@test.com"
