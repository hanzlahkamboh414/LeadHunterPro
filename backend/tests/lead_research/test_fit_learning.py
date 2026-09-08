"""FitLearningStore — the self-correcting fit loop (CLAUDE.md §3/§7/§8/§11).

Two halves:

* store unit tests — record/get/all/reset, the ``should_skip`` MIN_TRIALS +
  default-keep contract, key normalization, and durability across instances
  (the loop lives in the dossier DB, so it must survive a reopen).
* agent integration — the Stage-1c shortcut fired by the AI's ``is_our_client``
  verdict AND by the learned "never converts" signal, each proven to stop the
  pipeline BEFORE the expensive person/deep/intent/scoring lanes run, and each
  recording its own outcome back into the loop.
"""

from __future__ import annotations

from app.company_profile import get_profile
from app.lead_research.agent import AILeadResearchAgent
from app.lead_research.company_research import CompanyResearcher
from app.lead_research.fit_learning import (
    KIND_INDUSTRY,
    KIND_SOURCE,
    MIN_TRIALS,
    FitLearningStore,
    normalize_industry,
    source_host,
)
from app.lead_research.intent_timing import IntentTimingAnalyzer
from app.lead_research.models import PersonFindings
from app.lead_research.person_research_ai import PersonResearcherAI
from app.lead_research.scoring import LeadScorer
from tests.lead_research.conftest import (
    make_fake_ai,
    make_fake_fetch,
    make_fake_refine,
    make_fake_search,
)

# Deterministic MX stub — never a real lookup offline.
_MX_OK = lambda d: True  # noqa: E731


# ---------------------------------------------------------------------------
# normalize_industry
# ---------------------------------------------------------------------------

def test_normalize_industry_collapses_and_lowercases():
    assert normalize_industry("  General   Contractor ") == "general contractor"


def test_normalize_industry_keeps_compound_labels_distinct():
    """A compound label is NOT split on / or - — merging it with the plain
    label would let a converting class mask a non-converting one."""
    assert normalize_industry("General Contractor / Developer") == "general contractor / developer"
    assert normalize_industry("") == ""


def test_normalize_industry_caps_length():
    assert len(normalize_industry("x" * 200)) == 80


# ---------------------------------------------------------------------------
# source_host
# ---------------------------------------------------------------------------

def test_source_host_strips_scheme_www_port_and_userinfo():
    assert source_host("https://www.dcta.net/sites/x.pdf") == "dcta.net"
    assert source_host("http://plancenter.example:8080/list") == "plancenter.example"
    assert source_host("https://user@host.com/x") == "host.com"
    assert source_host("plaincenter.org/path") == "plaincenter.org"  # no scheme
    assert source_host("") == ""


# ---------------------------------------------------------------------------
# FitLearningStore — record / get / all / reset
# ---------------------------------------------------------------------------

def test_record_accumulates_and_get(tmp_path):
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.record(KIND_INDUSTRY, "gc", kept=True)
    s.record(KIND_INDUSTRY, "gc", kept=False)
    assert s.get(KIND_INDUSTRY, "gc") == {"trials": 2, "kept": 1}
    assert s.get(KIND_INDUSTRY, "unknown") is None


def test_empty_key_is_noop(tmp_path):
    """An unknown industry / opaque source is never learned — silence is not
    evidence (CLAUDE.md §6)."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.record(KIND_INDUSTRY, "", kept=True)
    assert s.get(KIND_INDUSTRY, "") is None
    assert s.should_skip(KIND_INDUSTRY, "") is False


def test_all_filters_by_kind(tmp_path):
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.record(KIND_INDUSTRY, "gc", kept=False)
    s.record(KIND_SOURCE, "dcta.net", kept=False)
    assert s.all(KIND_INDUSTRY) == {"industry:gc": {"trials": 1, "kept": 0}}
    assert set(s.all()) == {"industry:gc", "source:dcta.net"}


def test_reset_clears(tmp_path):
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.record(KIND_INDUSTRY, "gc", kept=False)
    s.reset()
    assert s.get(KIND_INDUSTRY, "gc") is None


def test_persists_across_instances(tmp_path):
    """The loop shares the dossier DB — a fresh store on the same path must see
    prior learning (durability: research keeps teaching the gate over time)."""
    db = str(tmp_path / "f.db")
    FitLearningStore(db).record_industry("gc", kept=True)
    assert FitLearningStore(db).get(KIND_INDUSTRY, "gc") == {"trials": 1, "kept": 1}


# ---------------------------------------------------------------------------
# FitLearningStore.should_skip — MIN_TRIALS + default-keep contract
# ---------------------------------------------------------------------------

def test_should_skip_keeps_below_min_trials_then_prunes(tmp_path):
    s = FitLearningStore(str(tmp_path / "f.db"))
    for _ in range(MIN_TRIALS - 1):
        s.record(KIND_INDUSTRY, "junk", kept=False)
    assert s.should_skip(KIND_INDUSTRY, "junk") is False  # not enough evidence yet
    s.record(KIND_INDUSTRY, "junk", kept=False)  # reaches MIN_TRIALS
    assert s.should_skip(KIND_INDUSTRY, "junk") is True


def test_should_skip_keeps_when_ever_converted(tmp_path):
    """A single real lead anywhere in the history keeps the class alive —
    default-keep, never starve the funnel on a class that has produced."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    for _ in range(MIN_TRIALS - 1):
        s.record(KIND_INDUSTRY, "mixed", kept=False)
    s.record(KIND_INDUSTRY, "mixed", kept=True)
    assert s.get(KIND_INDUSTRY, "mixed") == {"trials": MIN_TRIALS, "kept": 1}
    assert s.should_skip(KIND_INDUSTRY, "mixed") is False


def test_should_skip_unknown_is_false(tmp_path):
    s = FitLearningStore(str(tmp_path / "f.db"))
    assert s.should_skip(KIND_INDUSTRY, "never-seen") is False


def test_industry_wrappers_normalize(tmp_path):
    s = FitLearningStore(str(tmp_path / "f.db"))
    for _ in range(MIN_TRIALS):
        s.record_industry("  Fiber   Optics ", kept=False)
    assert s.get(KIND_INDUSTRY, "fiber optics") == {"trials": MIN_TRIALS, "kept": 0}
    assert s.should_skip_industry("Fiber Optics") is True
    assert s.should_skip_industry("fiber   optics") is True  # ws collapses to same key


def test_source_wrappers_normalize(tmp_path):
    s = FitLearningStore(str(tmp_path / "f.db"))
    for _ in range(MIN_TRIALS):
        s.record_source("https://www.dcta.net/list.pdf", kept=False)
    assert s.get(KIND_SOURCE, "dcta.net") == {"trials": MIN_TRIALS, "kept": 0}
    assert s.should_skip_source("http://dcta.net/other-list") is True


# ---------------------------------------------------------------------------
# Agent integration — Stage 1c shortcut driven by the loop
# ---------------------------------------------------------------------------

def _build_agent(store, *, company_response, person_spy=None):
    """An agent wired to ``store`` with all seams faked.

    ``person_spy`` (a ``{"n": 0}`` dict) replaces person research with a counter
    so a test can prove the person/deep/intent/scoring lanes never ran once the
    Stage 1c shortcut fires. When omitted, the real fake person lane binds a
    contact so the full pipeline can reach a scored recommendation.
    """
    company = CompanyResearcher(
        ai_ask=make_fake_ai(company_response),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    # Deep lane is deterministic here (no growth signals) — keeps the full-run
    # test fast and independent of the multi-key deep-research path.
    company.research_deep = lambda domain, company_name, **kw: []

    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "Jane Doe",
            "person_role": "Project Manager",
            "role_relevance": True,
            "bound": True,
            "evidence": [{"claim": "Team page", "source_url": "https://acme.com/team", "source_type": "website", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    if person_spy is not None:
        def _spy(**kwargs):
            person_spy["n"] += 1
            return PersonFindings()
        person.research = _spy

    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "yes", "signal": "Active bids", "reason": "Regularly bids",
        "evidence": [{"claim": "Bid activity", "source_url": "https://acme.com/bids", "source_type": "website", "confidence": "verified"}],
        "timing_window": "now", "timing_reason": "Q1 season",
        "timing_events": [{"claim": "RFP open", "source_url": "", "source_type": "inferred", "confidence": "unverified"}],
    }))
    scorer = LeadScorer(ai_ask=make_fake_ai({
        "fit": "Strong fit — active GC", "potential_score": 7.5,
        "recommendation": "contact_now", "reasoning": "Good match",
    }))

    return AILeadResearchAgent(
        company_researcher=company, person_researcher=person,
        intent_analyzer=intent, scorer=scorer,
        domain_delivers_email=_MX_OK, fit_learning_store=store,
    )


def _gc_response(**extra):
    """A clean General-Contractor Stage-1 response.

    GC is used deliberately: the deterministic backstop PASSES it (the
    happy-path pipeline test reaches contact_now on GC), so any skip here can
    only come from the AI verdict or the learned signal — isolating each path.
    """
    resp = {
        "company_name": "Acme Construction",
        "industry": "General Contractor",
        "location": "Dallas, TX",
        "website": "https://acme.com",
        "facts": [{"claim": "GC", "source_url": "https://acme.com/about", "source_type": "about_page", "confidence": "verified"}],
    }
    resp.update(extra)
    return resp


def test_gc_passes_deterministic_backstop():
    """Guard for the isolation the two skip tests rely on: GC is neither
    off-vertical nor a non-client class, so only AI/learned can skip it."""
    prof = get_profile()
    assert prof.is_off_vertical("General Contractor") is False
    assert prof.is_non_client("General Contractor") is False


def test_ai_verdict_no_skips_pipeline_and_records(tmp_path):
    """The Stage-1 AI verdict is_our_client == "no" stops the pipeline before
    person research and records the industry + source as a non-lead."""
    store = FitLearningStore(str(tmp_path / "f.db"))
    person_calls = {"n": 0}
    agent = _build_agent(
        store,
        company_response=_gc_response(
            is_our_client="no",
            client_reason="They self-estimate in-house; not a buyer.",
        ),
        person_spy=person_calls,
    )
    dossier = agent.research(
        "jane@acme.com", "acme.com",
        source_url="https://plancenter.example/list.pdf",
    )

    assert dossier.recommendation == "skip"
    assert "not our client" in dossier.fit.lower()
    assert "self-estimate" in dossier.fit.lower()  # the AI's grounded reason is surfaced
    assert person_calls["n"] == 0  # person/deep/intent/scoring never started
    # Outcome recorded for both learning namespaces (kept=0 → a non-lead).
    assert store.get(KIND_INDUSTRY, "general contractor") == {"trials": 1, "kept": 0}
    assert store.get(KIND_SOURCE, "plancenter.example") == {"trials": 1, "kept": 0}


def test_ai_verdict_yes_runs_full_pipeline_and_records_kept(tmp_path):
    """is_our_client == "yes" (or absent) does NOT short-circuit — the full
    pipeline runs and a scored real lead is recorded as kept=1."""
    store = FitLearningStore(str(tmp_path / "f.db"))
    agent = _build_agent(store, company_response=_gc_response(is_our_client="yes"))
    dossier = agent.research("jane@acme.com", "acme.com")

    assert dossier.recommendation == "contact_now"
    assert "person_research" in dossier.sources_checked
    assert store.get(KIND_INDUSTRY, "general contractor") == {"trials": 1, "kept": 1}


def test_learned_industry_short_circuits_before_person(tmp_path):
    """An industry that completed MIN_TRIALS runs and never once converted is
    auto-skipped at Stage 1c — even with no negative AI verdict — and the person
    lane never runs. This is the self-correcting "kabi wahi ghalti na kare" loop."""
    store = FitLearningStore(str(tmp_path / "f.db"))
    for _ in range(MIN_TRIALS):
        store.record_industry("General Contractor", kept=False)
    assert store.should_skip_industry("General Contractor") is True

    person_calls = {"n": 0}
    # No is_our_client key → verdict "" → the AI path is NOT what skips here.
    agent = _build_agent(store, company_response=_gc_response(), person_spy=person_calls)
    dossier = agent.research("jane@acme.com", "acme.com")

    assert dossier.recommendation == "skip"
    assert "auto-skipped" in dossier.fit.lower()
    assert person_calls["n"] == 0
    # The skip itself recorded one further non-lead trial.
    assert store.get(KIND_INDUSTRY, "general contractor") == {"trials": MIN_TRIALS + 1, "kept": 0}


def test_learning_disabled_never_skips_on_learned_signal(tmp_path):
    """With no store the loop is inert: an agent built without fit-learning runs
    the full pipeline (the feature is purely additive — CLAUDE.md §4)."""
    # Build an agent with fit_learning_store=None via the same seams.
    company = CompanyResearcher(
        ai_ask=make_fake_ai(_gc_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    company.research_deep = lambda domain, company_name, **kw: []
    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "Jane Doe", "person_role": "Project Manager",
            "role_relevance": True, "bound": True,
            "evidence": [{"claim": "Team", "source_url": "https://acme.com/team", "source_type": "website", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "yes", "signal": "", "reason": "", "evidence": [],
        "timing_window": "now", "timing_reason": "", "timing_events": [],
    }))
    scorer = LeadScorer(ai_ask=make_fake_ai({
        "fit": "fit", "potential_score": 7.5, "recommendation": "contact_now", "reasoning": "",
    }))
    agent = AILeadResearchAgent(
        company_researcher=company, person_researcher=person,
        intent_analyzer=intent, scorer=scorer,
        domain_delivers_email=_MX_OK,  # fit_learning_store omitted → None
    )
    dossier = agent.research("jane@acme.com", "acme.com")
    assert dossier.recommendation == "contact_now"  # no learning → no short-circuit
