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
    BUCKET_MARINE_HEAVY,
    BUCKET_ON_VERTICAL,
    BUCKET_OTHER,
    MIN_TRIALS,
    QueryYieldPlanner,
    QueryYieldStore,
    confirmed_bucket,
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
    d = agent.research("jane@acme.com", "acme.com")
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
    agent.research("jane@acme.com", "acme.com")

    row = store.get("screening:email")
    assert row is not None
    assert row["trials"] == MIN_TRIALS  # still 12 — no new dispatch happened
    assert row["cited"] == 0
    assert row["verified"] == 0


# ---------------------------------------------------------------------------
# Phase F — segment-level yield keys
# ---------------------------------------------------------------------------

# -- confirmed_bucket (deterministic, never from the AI label) ---------------

def test_confirmed_bucket_building_trade_is_on_vertical():
    assert confirmed_bucket(company="Acme Construction LLC", domain="acme.com") == BUCKET_ON_VERTICAL


def test_confirmed_bucket_marine_is_other_when_excluded():
    """The configured profile treats marine/heavy-civil as OFF-vertical (it is
    in ``excluded_vernaculars``), so P-C's exclusion rule wins → ``other``."""
    assert confirmed_bucket(company="Acme Marine Dredging", domain="acme-dredge.com") == BUCKET_OTHER


def test_confirmed_bucket_marine_branch_fires_when_not_excluded():
    """The marine_heavycivil bucket fires only for a marine term the profile
    does NOT exclude. Guarded, profile-driven (one source of truth)."""
    assert confirmed_bucket(company="Dockwright Contractors", domain="dockwright.com") == BUCKET_MARINE_HEAVY


def test_confirmed_bucket_off_vertical_is_other():
    assert confirmed_bucket(company="FiberWorks Telecom", domain="fiberworks.com") == BUCKET_OTHER


def test_confirmed_bucket_non_client_is_other():
    assert confirmed_bucket(company="Jacobs Consulting Group", domain="jacobs.com") == BUCKET_OTHER


def test_confirmed_bucket_ignores_ai_industry_label():
    """P-C — the bucket comes from company text, NOT the AI-written industry.
    Even if the AI mislabels this marine firm "GC", the marine text keeps it
    OUT of on_vertical (it is excluded) — the mislabel never contaminates the
    GC segment's yield."""
    assert confirmed_bucket(company="Gulf Marine Contractors", domain="gulfmarine.com") == BUCKET_OTHER


# -- store: segment rows + hierarchy -----------------------------------------

def test_store_segment_row_is_distinct_from_global(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    store.upsert("deep:license", segment="", trials=3, verified=0)
    store.upsert("deep:license", segment=BUCKET_ON_VERTICAL, trials=3, verified=1)
    # Global and segment rows are independent counts.
    assert store.get("deep:license", "") == {"trials": 3, "cited": 0, "verified": 0}
    assert store.get("deep:license", BUCKET_ON_VERTICAL) == {"trials": 3, "cited": 0, "verified": 1}
    # all() keys a segment row with the | separator; the global row keeps its name.
    assert store.all()["deep:license"]["trials"] == 3
    assert store.all()[f"deep:license|{BUCKET_ON_VERTICAL}"]["verified"] == 1


def test_should_skip_global_drop_is_authoritative(tmp_path):
    """Problem 2 — a global drop wins even when a segment has its own verified
    record: a dead template is never re-confirmed bucket-by-bucket."""
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    store.upsert("deep:license", segment="", trials=MIN_TRIALS, verified=0)  # global DROP
    store.upsert("deep:license", segment=BUCKET_ON_VERTICAL, trials=MIN_TRIALS, verified=5)
    assert store.should_skip("deep:license", BUCKET_ON_VERTICAL) is True


def test_should_skip_segment_decides_after_its_own_min_trials(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    store.upsert("deep:license", segment="", trials=MIN_TRIALS, verified=1)  # global KEEP
    store.upsert("deep:license", segment=BUCKET_ON_VERTICAL, trials=MIN_TRIALS - 1, verified=0)
    # Segment has NOT reached its own MIN_TRIALS -> falls back to global KEEP.
    assert store.should_skip("deep:license", BUCKET_ON_VERTICAL) is False
    # Once the segment reaches MIN_TRIALS with zero verified, it drops itself.
    store.upsert("deep:license", segment=BUCKET_ON_VERTICAL, trials=1, verified=0)
    assert store.should_skip("deep:license", BUCKET_ON_VERTICAL) is True


def test_should_skip_screening_stays_global(tmp_path):
    """Screening templates are gated GLOBALLY forever — no segment row gates them."""
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    store.upsert("screening:email", segment="", trials=MIN_TRIALS, verified=0)
    assert store.should_skip("screening:email", "") is True
    assert store.should_skip("screening:email", BUCKET_ON_VERTICAL) is True  # global still wins


def test_delete_template_kills_all_segments(tmp_path):
    """P-G — manual resurrection path: deleting a template clears every segment."""
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    store.upsert("deep:license", segment="", trials=MIN_TRIALS, verified=0)
    store.upsert("deep:license", segment=BUCKET_ON_VERTICAL, trials=MIN_TRIALS, verified=0)
    store.delete_template("deep:license")
    assert store.all() == {}
    assert store.should_skip("deep:license") is False


def test_legacy_table_migrates_to_segment(tmp_path):
    """Phase F migration: a live table with the old single-column (template) PK
    is rebuilt to (template, segment); legacy rows become the global row."""
    import sqlite3

    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE query_template_yield (
            template TEXT PRIMARY KEY,
            trials INTEGER NOT NULL DEFAULT 0,
            cited INTEGER NOT NULL DEFAULT 0,
            verified INTEGER NOT NULL DEFAULT 0,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO query_template_yield (template, trials, cited, verified) VALUES (?, ?, ?, ?)",
        ("deep:news", 7, 2, 0),
    )
    conn.commit()
    conn.close()

    store = QueryYieldStore(db)
    # Legacy row survived as the GLOBAL row (segment='').
    assert store.get("deep:news", "") == {"trials": 7, "cited": 2, "verified": 0}
    # And the widened PK now accepts a segment row.
    store.upsert("deep:news", segment=BUCKET_ON_VERTICAL, trials=1, verified=0)
    assert store.get("deep:news", BUCKET_ON_VERTICAL) == {"trials": 1, "cited": 0, "verified": 0}


# -- planner: deep writes global + segment rows ------------------------------

def test_planner_deep_note_writes_global_and_segment(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    p = QueryYieldPlanner(store)
    p.note("deep:license", ["https://a.com"], segment=BUCKET_ON_VERTICAL)
    p.commit(cited_urls={"https://a.com"}, verified_urls={"https://a.com"})
    # One run credits BOTH the global row and the on_vertical segment row.
    assert store.get("deep:license", "") == {"trials": 1, "cited": 1, "verified": 1}
    assert store.get("deep:license", BUCKET_ON_VERTICAL) == {"trials": 1, "cited": 1, "verified": 1}


def test_planner_screening_note_writes_global_only(tmp_path):
    store = QueryYieldStore(str(tmp_path / "yield.db"))
    p = QueryYieldPlanner(store)
    p.note("screening:email", ["https://a.com"])  # segment defaults to "" (global)
    p.commit(cited_urls={"https://a.com"}, verified_urls=set())
    assert store.get("screening:email", "") == {"trials": 1, "cited": 1, "verified": 0}
    # No segment row was created for a screening template.
    assert BUCKET_ON_VERTICAL not in store.all()


# -- agent integration: deep research writes segment rows --------------------

def test_agent_records_segment_rows_for_deep_templates(tmp_path):
    """A GC deep run credits deep templates under BOTH the global row and the
    on_vertical segment row — so deep:license's GC yield is measured apart from
    other buckets (the Phase F point)."""
    store = QueryYieldStore(str(tmp_path / "lead_research.db"))
    agent = _build_agent(store)
    agent.research("jane@acme.com", "acme.com")

    rows = store.all()
    # The deep templates were dispatched for an on_vertical (GC) company.
    for tpl in ("deep:license", "deep:news", "deep:hiring", "deep:expansion", "deep:bidaward"):
        assert f"{tpl}|{BUCKET_ON_VERTICAL}" in rows, f"missing segment row for {tpl}"
        assert rows[tpl]["trials"] >= 1  # global row credited too
        assert rows[f"{tpl}|{BUCKET_ON_VERTICAL}"]["trials"] >= 1