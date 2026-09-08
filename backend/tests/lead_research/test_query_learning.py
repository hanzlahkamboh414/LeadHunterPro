"""Query-yield learn loop (Phase 2B) — deterministic, zero extra AI calls.

The loop records per-search-TEMPLATE whether the URLs it returned were cited
as evidence (and, of those, as VERIFIED). After MIN_TRIALS dispatched runs
with zero verified citations the template is auto-dropped, so a future lead
never spends a provider credit on it.
"""

from __future__ import annotations

import json

from app.lead_research.agent import AILeadResearchAgent
from app.lead_research.company_research import CompanyResearcher
from app.lead_research.intent_timing import IntentTimingAnalyzer
from app.lead_research.query_learning import (
    MIN_TRIALS,
    QueryYieldPlanner,
    QueryYieldStore,
)
from app.lead_research.scoring import LeadScorer
from tests.lead_research.conftest import (
    make_fake_ai,
    make_fake_fetch,
    make_fake_refine,
    make_fake_search,
)
from app.lead_research.person_research_ai import PersonResearcherAI

_MX_OK = lambda d: True  # noqa: E731 — deterministic test stub, never live MX


# ---------------------------------------------------------------------------
# QueryYieldStore
# ---------------------------------------------------------------------------

def test_store_upsert_accumulates(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    store.upsert("screening:email", trials=1, cited=0, verified=0)
    store.upsert("screening:email", trials=1, cited=1, verified=1)
    row = store.get("screening:email")
    assert row == {"trials": 2, "cited": 1, "verified": 1}


def test_store_get_missing_and_all(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    assert store.get("nope") is None
    store.upsert("deep:licence", trials=5, cited=0, verified=0)
    assert store.all()["deep:licence"]["trials"] == 5


def test_store_reset_clears(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    store.upsert("deep:licence", trials=1, cited=1, verified=1)
    store.reset()
    assert store.all() == {}


# ---------------------------------------------------------------------------
# should_skip — the drop rule
# ---------------------------------------------------------------------------

def test_should_skip_keeps_until_min_trials(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    store.upsert("deep:news", trials=MIN_TRIALS - 1, cited=0, verified=0)
    assert store.should_skip("deep:news") is False


def test_should_skip_drops_zero_verified_after_min_trials(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    store.upsert("deep:news", trials=MIN_TRIALS, cited=1, verified=0)
    assert store.should_skip("deep:news") is True


def test_should_skip_keeps_verified_template(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    store.upsert("screening:linkedin_company", trials=MIN_TRIALS * 2, verified=5)
    assert store.should_skip("screening:linkedin_company") is False


def test_should_skip_unknown_template(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    assert store.should_skip("never_run") is False


# ---------------------------------------------------------------------------
# QueryYieldPlanner
# ---------------------------------------------------------------------------

def test_planner_disabled_without_store():
    p = QueryYieldPlanner()
    assert p.enabled is False
    p.note("x", ["https://a.com"])
    p.commit({"https://a.com"}, {"https://a.com"})  # must be a no-op


def test_planner_credits_cited_and_verified(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    p = QueryYieldPlanner(store)
    p.note("screening:email", ["https://a.com", "https://b.com"])
    p.note("screening:company", ["https://b.com"])
    p.note("deep:news", ["https://other.com"])
    p.commit(
        cited_urls={"https://a.com", "https://b.com"},
        verified_urls={"https://b.com"},
    )
    assert store.get("screening:email") == {"trials": 1, "cited": 1, "verified": 1}
    assert store.get("screening:company") == {"trials": 1, "cited": 1, "verified": 1}
    assert store.get("deep:news") == {"trials": 1, "cited": 0, "verified": 0}


def test_planner_note_dedupes_urls(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    p = QueryYieldPlanner(store)
    p.note("x", ["https://a.com", "https://a.com"])
    p.commit({"https://a.com"}, set())
    assert store.get("x") == {"trials": 1, "cited": 1, "verified": 0}


def test_planner_should_skip_delegates(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    store.upsert("deep:news", trials=MIN_TRIALS, verified=0)
    p = QueryYieldPlanner(store)
    assert p.should_skip("deep:news") is True
    assert p.should_skip("anything_else") is False


# ---------------------------------------------------------------------------
# Agent integration — a real research() dispatches templates and commits
# ---------------------------------------------------------------------------

def _build_agent(store):
    company_ai = {
        "company_name": "Acme Construction",
        "industry": "General Contractor",
        "location": "Dallas, TX",
        "website": "https://example.com",
        "facts": [
            {"claim": "Estimating services needed", "source_url": "https://example.com/about",
             "source_type": "website", "confidence": "verified"},
        ],
    }
    company = CompanyResearcher(
        ai_ask=make_fake_ai(company_ai),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("example.com"),
    )
    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "Jane Doe",
            "person_role": "Estimator",
            "role_relevance": True,
            "bound": True,
            "evidence": [{"claim": "Estimator at Acme", "source_url": "https://example.com/about",
                          "source_type": "website", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "yes",
        "signal": "Active bids",
        "reason": "Regularly bids",
        "evidence": [{"claim": "Bid activity", "source_url": "https://example.com/about",
                      "source_type": "website", "confidence": "verified"}],
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
        domain_delivers_email=_MX_OK,
        query_yield_store=store,
    )


def test_agent_records_yield_for_dispached_templates(tmp_path):
    store = QueryYieldStore(str(tmp_path / "lead_research.db"))
    agent = _build_agent(store)
    d = agent.research("jane@example.com", "example.com")
    assert d.person.bound is True  # full pipeline ran

    rows = store.all()
    # Screens all 4 templates dispatch on a normal run.
    assert "screening:email" in rows
    assert "screening:domain" in rows
    assert "screening:company" in rows
    assert "screening:linkedin_company" in rows
    # Company + person + intent all cite example.com/about; every template's
    # fake search returns it, so each credited run is cited + verified.
    for key in ("screening:email", "screening:company", "screening:linkedin_company"):
        row = rows[key]
        assert row["trials"] >= 1
        assert row["cited"] >= 1
        assert row["verified"] >= 1


def test_agent_prunes_zero_verified_template(tmp_path):
    """A template already proven zero-verified is pruned BEFORE dispatch: its
    trial count must NOT grow on the next research() call."""
    store = QueryYieldStore(str(tmp_path / "lead_research.db"))
    store.upsert("screening:email", trials=MIN_TRIALS, cited=0, verified=0)

    agent = _build_agent(store)
    agent.research("jane@example.com", "example.com")

    row = store.get("screening:email")
    assert row is not None
    assert row["trials"] == MIN_TRIALS  # still 12 — no new dispatch happened
    assert row["cited"] == 0
    assert row["verified"] == 0