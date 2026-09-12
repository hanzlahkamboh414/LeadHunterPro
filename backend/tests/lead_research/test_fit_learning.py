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
    KIND_COMPANY,
    KIND_DOMAIN,
    KIND_INDUSTRY,
    KIND_SOURCE,
    MIN_TRIALS,
    PROP_MIN_TRIALS,
    FitLearningStore,
    mail_domain,
    normalize_company,
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


#: A full store row — get()/all() now carry the corroboration fields too.
def _row(trials, kept, user_rejects=0, rejector_ids="", corroborated=0):
    return {
        "trials": trials, "kept": kept, "user_rejects": user_rejects,
        "rejector_ids": rejector_ids, "corroborated": corroborated,
    }


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
    assert s.get(KIND_INDUSTRY, "gc") == _row(2, 1)
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
    assert s.all(KIND_INDUSTRY) == {"industry:gc": _row(1, 0)}
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
    assert FitLearningStore(db).get(KIND_INDUSTRY, "gc") == _row(1, 1)


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
    assert s.get(KIND_INDUSTRY, "mixed") == _row(MIN_TRIALS, 1)
    assert s.should_skip(KIND_INDUSTRY, "mixed") is False


def test_should_skip_unknown_is_false(tmp_path):
    s = FitLearningStore(str(tmp_path / "f.db"))
    assert s.should_skip(KIND_INDUSTRY, "never-seen") is False


# ---------------------------------------------------------------------------
# should_skip — proportional prune (a stray keep must not immunize chronic waste)
# ---------------------------------------------------------------------------

def test_proportional_prune_drops_chronic_low_performer(tmp_path):
    """The live failure this fixes: media.governmentnavigator.com sat at 51
    trials / 2 kept (4%) forever because the 0-kept rule never fires on a
    stray keep. At PROP_MIN_TRIALS a poor rate is proven junk."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    for _ in range(49):
        s.record(KIND_SOURCE, "govnav.org", kept=False)
    s.record(KIND_SOURCE, "govnav.org", kept=True)
    s.record(KIND_SOURCE, "govnav.org", kept=False)
    assert s.get(KIND_SOURCE, "govnav.org") == _row(51, 1)
    assert s.should_skip_source("https://govnav.org/page") is True


def test_proportional_prune_keeps_marginal_producer(tmp_path):
    """7/40 = 17.5% sits above the threshold — a borderline producer stays
    alive (goldengate.org's live 17% survives too). Default-keep stands
    until the rate is genuinely poor."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    for _ in range(33):
        s.record(KIND_SOURCE, "marginal.org", kept=False)
    for _ in range(7):
        s.record(KIND_SOURCE, "marginal.org", kept=True)
    assert s.get(KIND_SOURCE, "marginal.org") == _row(40, 7)
    assert s.should_skip_source("https://marginal.org/") is False


def test_proportional_prune_needs_full_evidence_below_threshold_trials(tmp_path):
    """A poor rate on FEW trials is not proof: 20 trials at 10% keeps the
    class alive until PROP_MIN_TRIALS (the same default-keep philosophy as
    MIN_TRIALS — silence and small samples never prune)."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    for _ in range(18):
        s.record(KIND_INDUSTRY, "engineering firm", kept=False)
    s.record(KIND_INDUSTRY, "engineering firm", kept=True)
    s.record(KIND_INDUSTRY, "engineering firm", kept=False)
    assert s.get(KIND_INDUSTRY, "engineering firm") == _row(20, 1)
    assert s.should_skip_industry("engineering firm") is False


def test_industry_wrappers_normalize(tmp_path):
    s = FitLearningStore(str(tmp_path / "f.db"))
    for _ in range(MIN_TRIALS):
        s.record_industry("  Fiber   Optics ", kept=False)
    assert s.get(KIND_INDUSTRY, "fiber optics") == _row(MIN_TRIALS, 0)
    assert s.should_skip_industry("Fiber Optics") is True
    assert s.should_skip_industry("fiber   optics") is True  # ws collapses to same key


def test_source_wrappers_normalize(tmp_path):
    s = FitLearningStore(str(tmp_path / "f.db"))
    for _ in range(MIN_TRIALS):
        s.record_source("https://www.dcta.net/list.pdf", kept=False)
    assert s.get(KIND_SOURCE, "dcta.net") == _row(MIN_TRIALS, 0)
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
    assert store.get(KIND_INDUSTRY, "general contractor") == _row(1, 0)
    assert store.get(KIND_SOURCE, "plancenter.example") == _row(1, 0)


def test_ai_verdict_yes_runs_full_pipeline_and_records_kept(tmp_path):
    """is_our_client == "yes" (or absent) does NOT short-circuit — the full
    pipeline runs and a scored real lead is recorded as kept=1."""
    store = FitLearningStore(str(tmp_path / "f.db"))
    agent = _build_agent(store, company_response=_gc_response(is_our_client="yes"))
    dossier = agent.research("jane@acme.com", "acme.com")

    assert dossier.recommendation == "contact_now"
    assert "person_research" in dossier.sources_checked
    assert store.get(KIND_INDUSTRY, "general contractor") == _row(1, 1)


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
    assert store.get(KIND_INDUSTRY, "general contractor") == _row(MIN_TRIALS + 1, 0)


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


# ---------------------------------------------------------------------------
# Phase E — company/domain USER-verdict namespaces
# ---------------------------------------------------------------------------

def test_normalize_company_collapses_and_lowercases():
    assert normalize_company("  Acme   Construction LLC ") == "acme construction llc"


def test_normalize_company_keeps_legal_suffixes_distinct():
    """A legal suffix is NOT stripped — "Acme" and "Acme Construction LLC" stay
    distinct keys so one firm's rejection never hides a different valid lead."""
    assert normalize_company("Acme LLC") != normalize_company("Acme Construction LLC")
    assert normalize_company("ACME INC") == "acme inc"


def test_normalize_company_caps_length():
    assert len(normalize_company("x" * 500)) == 120


def test_mail_domain_normalizes_case_www_at_and_dots():
    assert mail_domain("WWW.Acme.Com") == "acme.com"
    assert mail_domain("@acme.com") == "acme.com"
    assert mail_domain("acme.com.") == "acme.com"
    assert mail_domain("") == ""


def test_reject_company_is_decisive_with_zero_trials(tmp_path):
    """A CORROBORATED rejection skips a company immediately — no MIN_TRIALS, no
    AI self-credit needed (Phase E: the research/admin-backed verdict is ground
    truth)."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    assert s.should_skip_company("Acme Construction") is False
    s.reject_company("  ACME   CONSTRUCTION LLC ", corroborated=True)
    assert s.get(KIND_COMPANY, "acme construction llc") == _row(
        0, 0, user_rejects=1, corroborated=1)
    assert s.should_skip_company("Acme Construction LLC") is True


def test_reject_accumulates_and_persists_across_instances(tmp_path):
    """Repeated rejections accumulate the audit counter (user_rejects=2) and a
    fresh store on the same path still sees the decisive verdict. The
    corroborated flag only ever moves UP (an admin backing an already-plain
    rejection makes it decisive; nothing un-corroborates it)."""
    db = str(tmp_path / "f.db")
    FitLearningStore(db).reject_company("Acme Construction", user_id="u1")
    FitLearningStore(db).reject_company("Acme Construction", user_id="u1",
                                        corroborated=True)
    s = FitLearningStore(db)
    assert s.get(KIND_COMPANY, "acme construction") == _row(
        0, 0, user_rejects=2, rejector_ids="u1", corroborated=1)
    assert s.should_skip_company("Acme Construction") is True


def test_reject_domain_decisive_case_and_www_insensitive(tmp_path):
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.reject_domain("WWW.Acme.Com.", corroborated=True)
    assert s.should_skip_domain("acme.com") is True
    assert s.should_skip_domain("@Acme.Com") is True


def test_record_on_company_namespace_does_not_skip(tmp_path):
    """An AI self-reported 'kept' outcome on the company namespace must NOT be
    treated as a user rejection — only record() learns it, not reject(), and
    should_skip ignores trials/kept for company/domain (no AI self-credit)."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.record(KIND_COMPANY, "acme construction", kept=True)
    s.record(KIND_COMPANY, "acme construction", kept=False)
    assert s.get(KIND_COMPANY, "acme construction") == _row(2, 1)
    assert s.should_skip_company("Acme Construction") is False


def test_domain_rejection_isolated_from_industry_and_source(tmp_path):
    """A rejected domain prunes only that domain — the industry and source it
    belongs to are untouched (never over-prune the funnel on one verdict)."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.record_industry("General Contractor", kept=True)
    s.record_source("https://dcta.net/list.pdf", kept=True)
    s.reject_domain("acme.com", corroborated=True)
    assert s.should_skip_domain("acme.com") is True
    assert s.should_skip_industry("General Contractor") is False
    assert s.should_skip_source("https://dcta.net/") is False


def test_company_rejection_isolated_from_domain(tmp_path):
    """Rejecting a company name prunes that name only — the domain stays alive
    (a different firm may legitimately sit on the same mail host)."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.reject_company("Acme Construction", corroborated=True)
    assert s.should_skip_company("Acme Construction") is True
    assert s.should_skip_domain("acme.com") is False


def test_migration_adds_user_rejects_to_old_table(tmp_path):
    """A live DB whose fit_learning table predates user_rejects must gain the
    column on init, and reject() must then work (Phase E ships additive)."""
    import sqlite3

    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE fit_learning (
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            trials INTEGER NOT NULL DEFAULT 0,
            kept INTEGER NOT NULL DEFAULT 0,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (kind, key)
        )
        """
    )
    conn.execute(
        "INSERT INTO fit_learning (kind, key, trials, kept) VALUES (?, ?, ?, ?)",
        (KIND_INDUSTRY, "gc", 5, 0),
    )
    conn.commit()
    conn.close()

    s = FitLearningStore(db)
    # Legacy research row survived with user_rejects defaulting to 0 (not a verdict).
    assert s.get(KIND_INDUSTRY, "gc") == _row(5, 0)
    assert s.should_skip_industry("gc") is False  # 5 trials < MIN_TRIALS, no verdict
    # The migrated column now accepts a decisive user verdict.
    s.reject_company("Acme Construction", corroborated=True)
    assert s.should_skip_company("Acme Construction") is True


def test_migration_adds_corroboration_columns_to_phase_e_table(tmp_path):
    """A live DB whose fit_learning table predates the gaming guard (has
    user_rejects but NOT rejector_ids/corroborated) must gain both columns on
    init. Pre-guard company/domain rows default to corroborated=0 /
    rejector_ids='' — an uncorroborated lone rejection — so they stop being
    decisive until a second distinct user or a research-backed verdict arrives."""
    import sqlite3

    db = str(tmp_path / "phase-e.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE fit_learning (
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            trials INTEGER NOT NULL DEFAULT 0,
            kept INTEGER NOT NULL DEFAULT 0,
            user_rejects INTEGER NOT NULL DEFAULT 0,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (kind, key)
        )
        """
    )
    # A pre-guard decisive row: 1 rejection, no user id recorded.
    conn.execute(
        "INSERT INTO fit_learning (kind, key, trials, kept, user_rejects) "
        "VALUES (?, ?, 0, 0, 1)",
        (KIND_COMPANY, "old rejected co"),
    )
    conn.commit()
    conn.close()

    s = FitLearningStore(db)
    row = s.get(KIND_COMPANY, "old rejected co")
    assert row == _row(0, 0, user_rejects=1, rejector_ids="", corroborated=0)
    # The gaming guard reads the migrated row honestly: the legacy verdict's
    # rejector is ANONYMOUS (no id was recorded pre-guard), so one later named
    # user still leaves only one distinct named rejector — not decisive.
    assert s.should_skip_company("Old Rejected Co") is False
    s.reject_company("Old Rejected Co", user_id="u2")
    assert s.get(KIND_COMPANY, "old rejected co") == _row(
        0, 0, user_rejects=2, rejector_ids="u2")
    assert s.should_skip_company("Old Rejected Co") is False
    # The migration path for such rows is the backfill script: it replays the
    # admin-confirmed purge as corroborated, which IS decisive.
    s.reject_company("Old Rejected Co", user_id="u2", corroborated=True)
    assert s.should_skip_company("Old Rejected Co") is True


# ---------------------------------------------------------------------------
# Gaming guard (2026-09-12) — is the "not our client" click the TRUTH?
# ---------------------------------------------------------------------------

def test_uncorroborated_single_user_rejection_is_not_decisive(tmp_path):
    """One user clicking the strong reason on a lead the research itself LIKED
    (no corroborated flag) must NOT purge the identity globally — the user may
    just be tidying up ("sirf safai"). The verdict is recorded for audit, but
    the gate stays open."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.reject_company("Acme Construction", user_id="u1")
    s.reject_domain("acme.com", user_id="u1")
    assert s.get(KIND_COMPANY, "acme construction") == _row(
        0, 0, user_rejects=1, rejector_ids="u1")
    assert s.should_skip_company("Acme Construction") is False
    assert s.should_skip_domain("acme.com") is False


def test_same_user_rejecting_twice_is_still_not_decisive(tmp_path):
    """The guard counts DISTINCT users — u1 clicking "not our client" on every
    email the company owns is still one opinion, not two. Duplicate clicks only
    accumulate the audit counter (user_rejects), never the rejector set."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    for _ in range(3):
        s.reject_company("Acme Construction", user_id="u1")
    assert s.get(KIND_COMPANY, "acme construction") == _row(
        0, 0, user_rejects=3, rejector_ids="u1")
    assert s.should_skip_company("Acme Construction") is False


def test_second_distinct_user_rejection_is_decisive(tmp_path):
    """TWO INDEPENDENT users choosing the same identity is strong evidence the
    verdict is real, not a cleaning spree — that is decisive."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.reject_company("Acme Construction", user_id="u1")
    assert s.should_skip_company("Acme Construction") is False
    s.reject_company("Acme Construction", user_id="u2")
    assert s.get(KIND_COMPANY, "acme construction") == _row(
        0, 0, user_rejects=2, rejector_ids="u1,u2")
    assert s.should_skip_company("Acme Construction") is True


def test_corroborated_rejection_is_immediately_decisive(tmp_path):
    """When the research itself agreed (the dossier's fit said "not our
    client") or an admin made the call, ONE verdict is decisive — the user's
    click merely confirms evidence that already existed."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.reject_company("Acme Construction", user_id="u1", corroborated=True)
    s.reject_domain("acme.com", user_id="u1", corroborated=True)
    assert s.should_skip_company("Acme Construction") is True
    assert s.should_skip_domain("acme.com") is True


def test_corroboration_cannot_be_unset_by_later_plain_rejections(tmp_path):
    """corroborated only moves UP: a later uncorroborated delete (any user)
    must never downgrade a research-backed purge back to open."""
    s = FitLearningStore(str(tmp_path / "f.db"))
    s.reject_company("Acme Construction", user_id="u1", corroborated=True)
    s.reject_company("Acme Construction", user_id="u2")
    row = s.get(KIND_COMPANY, "acme construction")
    assert row["user_rejects"] == 2
    assert row["corroborated"] == 1
    assert s.should_skip_company("Acme Construction") is True


# ---------------------------------------------------------------------------
# Phase E — agent Stage 1c short-circuits on the user-rejection signal
# ---------------------------------------------------------------------------

def test_user_rejected_company_short_circuits_before_person(tmp_path):
    """A company the user already deleted as not-a-client is auto-skipped at
    Stage 1c — before person research — and the reason names the purge."""
    store = FitLearningStore(str(tmp_path / "f.db"))
    store.reject_company("Acme Construction", corroborated=True)
    assert store.should_skip_company("Acme Construction") is True

    person_calls = {"n": 0}
    # No is_our_client / no learned-industry — the ONLY skip signal is the
    # user's own rejection (isolating the Phase E path).
    agent = _build_agent(store, company_response=_gc_response(), person_spy=person_calls)
    dossier = agent.research("jane@acme.com", "acme.com")

    assert dossier.recommendation == "skip"
    assert "You marked this company" in dossier.fit
    assert "not-a-client" in dossier.fit
    assert person_calls["n"] == 0  # person/deep/intent/scoring never started


def test_user_rejected_domain_short_circuits_before_person(tmp_path):
    """A rejected mail domain auto-skips on the next pass even when the company
    name differs slightly — the domain is the durable purge key."""
    store = FitLearningStore(str(tmp_path / "f.db"))
    store.reject_domain("acme.com", corroborated=True)
    assert store.should_skip_domain("acme.com") is True

    person_calls = {"n": 0}
    agent = _build_agent(store, company_response=_gc_response(), person_spy=person_calls)
    dossier = agent.research("jane@acme.com", "acme.com")

    assert dossier.recommendation == "skip"
    assert "auto-skipped" in dossier.fit.lower()
    assert person_calls["n"] == 0
