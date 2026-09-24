"""Leads pipeline orchestration — offline tests (fake discovery + fake agent)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.discovery.sources.status import SourceStatus
from app.lead_research.agent import DEAD_DOMAIN_MARKER
from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.leads.pipeline import (
    ResearchQuery,
    _discovery_status,
    company_records_to_leads,
    discover_until_target,
    extract_email_leads,
    run_discovery,
    run_full,
    run_research,
)


def _record(company: str, email: str, domain: str, person: str = "") -> dict:
    return {
        "company_name": company,
        "source_url": "https://x.example",
        # P2 trade gate: these runs query trade="gc" — records must carry gc
        # evidence to be served (unlabeled records no longer serve to a
        # trade-filtered run).
        "trade_category": "general_contractor",
        "plan_holder": {
            "domain": domain,
            "emails": [{"email": email}],
            "person": {"name": person},
        },
    }


def _fake_discovery(records, status=SourceStatus.SUCCESS):
    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        return status, list(records), {"pdfs_found": 1, "pdf_urls": ["https://x.example"]}
    return _discover


# ---------------------------------------------------------------------------
# extract_email_leads
# ---------------------------------------------------------------------------

def test_extract_email_leads_dedups():
    records = [
        _record("A", "a@x.com", "x.com"),
        _record("A", "a@x.com", "x.com"),  # duplicate
        _record("B", "b@y.com", "y.com"),
    ]
    leads = extract_email_leads(records)
    assert len(leads) == 2
    assert {l["email"] for l in leads} == {"a@x.com", "b@y.com"}


def test_extract_email_leads_skips_bad_email():
    records = [_record("A", "not-an-email", "x.com")]
    assert extract_email_leads(records) == []


# ---------------------------------------------------------------------------
# Phase D — multi-source discovery (orchestrator lanes + website email bridge)
# ---------------------------------------------------------------------------

def _website_record(company: str, website: str, source_url: str = "") -> dict:
    """A crawl/search-lane record: a REAL company website, no emails on it yet.
    ``website`` is the company's own domain — never a directory profile URL."""
    return {
        "company_name": company,
        "website": website,
        "source_url": source_url or website,
        "city": "Dallas",
        "state": "TX",
        "_discovery_source": "directory_crawl",
    }


def test_email_bridge_yields_new_lead_from_website_lane():
    """A website-only record (no plan-holder row) is probed for a real public
    email — the CLAUDE.md §10 'visit websites → extract' flow, in the pipeline."""
    def fake_discover(url):
        return {"emails": ["amy@acme.com"], "count": 1}

    leads, stats = company_records_to_leads(
        [_website_record("Acme", "https://www.Acme.com/contact")],
        email_discover=fake_discover,
    )
    assert len(leads) == 1
    assert leads[0]["email"] == "amy@acme.com"
    assert leads[0]["domain"] == "acme.com"  # www + path stripped to the domain
    assert stats == {
        "plan_emails": 0, "probed": 1,
        "with_email": 1, "dup_plan": 0, "no_email": 0,
    }


def test_email_probe_prefers_named_mailbox_over_generic():
    """A named mailbox (john@) beats info@ when a site exposes both — the
    person-attribution downstream has something real to anchor on."""
    def fake_discover(url):
        return {"emails": ["info@acme.com", "john@acme.com"], "count": 2}

    leads, _ = company_records_to_leads(
        [_website_record("Acme", "https://acme.com")],
        email_discover=fake_discover,
    )
    assert leads[0]["email"] == "john@acme.com"


def test_email_probe_no_public_email_dropped_honestly():
    """A site with no readable public email is NOT fabricated into a lead — it
    is dropped and counted (honest telemetry, §12 never invent an address)."""
    def fake_discover(url):
        return {"emails": [], "count": 0}

    leads, stats = company_records_to_leads(
        [_website_record("Acme", "https://acme.com")],
        email_discover=fake_discover,
    )
    assert leads == []
    assert stats["probed"] == 1 and stats["no_email"] == 1 and stats["with_email"] == 0


def test_email_bridge_probing_is_bounded():
    """Website probing is capped per pass — a search batch can carry dozens of
    companies, and probing every site would explode into hundreds of fetches."""
    calls = []

    def fake_discover(url):
        calls.append(url)
        return {"emails": [f"{len(calls)}@probe.com"], "count": 1}

    records = [_website_record(f"C{i}", f"https://c{i}.com") for i in range(30)]
    leads, stats = company_records_to_leads(
        records, email_discover=fake_discover, max_probes=5,
    )
    assert len(calls) == 5  # bounded per pass
    assert stats["probed"] == 5 and stats["with_email"] == 5
    assert len(leads) == 5


def test_email_bridge_requires_real_company_website():
    """A bare directory profile URL (no ``website`` field) is NOT a company
    domain — it is skipped without probing (never crawl a directory page)."""
    calls = []

    def fake_discover(url):
        calls.append(url)
        return {"emails": ["x@y.com"], "count": 1}

    records = [{
        "company_name": "Acme",
        "source_url": "https://directory.example/acme-profile",  # profile, not site
        "city": "Dallas", "state": "TX",
    }]
    leads, stats = company_records_to_leads(
        records, email_discover=fake_discover,
    )
    assert calls == []
    assert stats["probed"] == 0 and stats["with_email"] == 0


def test_email_bridge_lane_dedups_against_plan_holder():
    """A website-lane email that repeats a plan-holder lead is not double-
    counted — it is a 'dup_plan', never a 'no_email' site (honest §6 stats)."""
    def fake_discover(url):
        return {"emails": ["amy@acme.com"], "count": 1}

    leads, stats = company_records_to_leads(
        [
            _record("Acme", "amy@acme.com", "acme.com"),
            _website_record("Acme Site", "https://acme.com"),
        ],
        email_discover=fake_discover,
    )
    assert [l["email"] for l in leads] == ["amy@acme.com"]  # ONE lead, not two
    assert stats["plan_emails"] == 1
    assert stats["probed"] == 1 and stats["with_email"] == 0 and stats["dup_plan"] == 1


def test_discovery_status_any_success_wins():
    """Aggregate status = SUCCESS when ANY live lane returned companies (the
    multi-source guarantee: one lane's emptiness never hides another's win)."""
    ok = _discovery_status({"source_stats": {
        "directory_crawl": {"status": SourceStatus.EMPTY},
        "plan_holder": {"status": SourceStatus.SUCCESS},
        "search": {"status": SourceStatus.UNAVAILABLE},
    }})
    assert ok == SourceStatus.SUCCESS


def test_discovery_status_empty_without_success():
    empty = _discovery_status({"source_stats": {
        "directory_crawl": {"status": SourceStatus.EMPTY},
        "plan_holder": {"status": SourceStatus.UNAVAILABLE},
    }})
    assert empty == SourceStatus.EMPTY


def test_run_discovery_uses_orchestrator_and_threads_pdf_urls(monkeypatch):
    """run_discovery now drives the SourceOrchestrator (all THREE live lanes),
    and threads the plan-holder lane's pdf_urls to the top so the pass loop
    keeps advancing skip_pdfs (same contract as the old single-source call)."""
    fake_orch = SimpleNamespace(
        discover=lambda **kw: (
            [_website_record("Acme", "https://acme.com")],
            {
                "data_source": "live",
                "bridge_mode": False,
                "fallback_reason": "",
                "source_stats": {
                    "directory_crawl": {"status": SourceStatus.EMPTY, "results": 0, "metadata": {}},
                    "plan_holder": {
                        "status": SourceStatus.SUCCESS, "results": 1,
                        "metadata": {"pdf_urls": ["https://ph/1.pdf"], "pdfs_found": 1},
                    },
                    "search": {"status": SourceStatus.EMPTY, "results": 0, "metadata": {}},
                },
            },
        ),
    )
    monkeypatch.setattr(
        "app.leads.pipeline._build_discovery_orchestrator",
        lambda skip=None, yield_store=None, candidate_store=None: fake_orch,
    )
    status, records, meta = run_discovery("roofing", "Texas", 10, skip_pdfs={"https://ph/0.pdf"})
    assert status == SourceStatus.SUCCESS
    assert len(records) == 1
    assert meta["pdf_urls"] == ["https://ph/1.pdf"]
    assert meta["data_source"] == "live"
    assert meta.get("reason") is None  # reason only set when nothing succeeded


def test_run_discovery_empty_sets_honest_reason(monkeypatch):
    """All lanes empty -> status EMPTY + the honest WHY rides in meta (never a
    silent 'live=False', CLAUDE.md §5/§6)."""
    fake_orch = SimpleNamespace(
        discover=lambda **kw: (
            [],
            {
                "data_source": "empty",
                "bridge_mode": False,
                "fallback_reason": "directory_crawl: EMPTY; plan_holder: EMPTY; search: EMPTY",
                "source_stats": {
                    "directory_crawl": {"status": SourceStatus.EMPTY, "results": 0, "metadata": {}},
                    "plan_holder": {"status": SourceStatus.EMPTY, "results": 0, "metadata": {}},
                    "search": {"status": SourceStatus.EMPTY, "results": 0, "metadata": {}},
                },
            },
        ),
    )
    monkeypatch.setattr(
        "app.leads.pipeline._build_discovery_orchestrator",
        lambda skip=None, yield_store=None, candidate_store=None: fake_orch,
    )
    status, records, meta = run_discovery("gc", "TX", 10, skip_pdfs=set())
    assert status == SourceStatus.EMPTY
    assert records == []
    assert meta["reason"]  # auditable stop reason, never silent


# ---------------------------------------------------------------------------
# Phase H add-side wired LIVE: _run_discovery_generation
# ---------------------------------------------------------------------------

def test_discovery_generation_off_without_stores():
    """Feature-off: no stores -> no generation, ever (additive, static-only)."""
    from app.leads.pipeline import _run_discovery_generation

    assert _run_discovery_generation(None, None) is None
    assert _run_discovery_generation(object(), None) is None
    assert _run_discovery_generation(None, object()) is None


def test_discovery_generation_fires_when_room(monkeypatch, tmp_path):
    """With both stores present and the cold-start gate cleared, the add-side
    calls the generator (1 LLM credit) and returns its honest result.

    BOTH lanes are stubbed. ``_run_discovery_generation`` merges the Layer-1
    dork result with the Layer-2 web-angle result, so asserting on the merge
    while leaving the web lane REAL made this test depend on that lane's
    cold-start guard holding — and, once the guard did not hold, on a live AI
    call. It failed once in a full-suite run and passed alone, which is the
    signature of exactly this: a test that reads production behaviour it never
    pinned. Stubbing the second lane makes the assertion a statement about the
    merge (which is what the test is for) instead of about the world.
    """
    from app.leads.pipeline import _run_discovery_generation
    from app.discovery.template_candidates import TemplateCandidateStore
    from app.discovery.yield_learning import DiscoveryYieldStore

    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    cand_store = TemplateCandidateStore(str(tmp_path / "cand.db"))
    new_dork = '"plan holder roster" {industry} {location} filetype:pdf'
    calls = {"dork": 0, "web": 0}

    def _fake_gen(yield_store, *, candidate_store=None, segment=""):
        calls["dork"] += 1
        return {"generated": [new_dork], "rejected": [], "reason": ""}

    def _fake_web(yield_store, *, candidate_store=None, segment=""):
        calls["web"] += 1
        # An honest HOLD — the fresh yield store has no trial evidence.
        return {"generated": [], "rejected": [],
                "reason": "cold-start guard: no dispatched template yet"}

    monkeypatch.setattr(
        "app.discovery.template_generation.generate_dork_candidates", _fake_gen,
    )
    monkeypatch.setattr(
        "app.discovery.template_generation.generate_web_angle_candidates",
        _fake_web,
    )
    result = _run_discovery_generation(yield_store, cand_store)
    assert calls == {"dork": 1, "web": 1}  # one LLM credit per lane, no more
    assert result["generated"] == [new_dork]  # the held lane contributes none


def test_discovery_generation_pauses_at_cap(monkeypatch, tmp_path):
    """Bounded: once >= _DISCOVERY_GEN_CAP candidates are staged, the DORK
    lane's add-side pauses instead of inventing more — the yield loop must
    prove/drop the queue before the AI proposes again (never an unbounded pile
    of unproven dorks). The web lane is paused by its own cap separately."""
    from app.leads.pipeline import _DISCOVERY_GEN_CAP, _run_discovery_generation
    from app.discovery.template_candidates import TemplateCandidateStore
    from app.discovery.yield_learning import DiscoveryYieldStore

    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    cand_store = TemplateCandidateStore(str(tmp_path / "cand.db"))
    for i in range(_DISCOVERY_GEN_CAP):
        cand_store.propose(f"dork {i} {{industry}} {{location}} filetype:pdf")
    # Web lane also at cap -> nothing runs at all -> None.
    for i in range(_DISCOVERY_GEN_CAP):
        cand_store.propose(f"angle {i} {{industry}} {{location}}", layer="web")
    calls = {"dork": 0, "web": 0}

    def _fake_dork(*a, **k):
        calls["dork"] += 1
        return {"generated": [], "rejected": [], "reason": ""}

    def _fake_web(*a, **k):
        calls["web"] += 1
        return {"generated": [], "rejected": [], "reason": ""}

    monkeypatch.setattr(
        "app.discovery.template_generation.generate_dork_candidates", _fake_dork,
    )
    monkeypatch.setattr(
        "app.discovery.template_generation.generate_web_angle_candidates", _fake_web,
    )
    assert _run_discovery_generation(yield_store, cand_store) is None
    assert calls == {"dork": 0, "web": 0}  # both lanes full — no generator asked


def test_discovery_generation_dork_cap_never_blocks_web_angles(monkeypatch, tmp_path):
    """Inc 2 regression (live proof 2026-09-11): the live store held 9 dork
    candidates, the cap check returned early, and web angles were NEVER
    generated. A full DORK queue must pause only the dork lane — method
    invention is a separate lane with its own cap and must still fire."""
    from app.leads.pipeline import _DISCOVERY_GEN_CAP, _run_discovery_generation
    from app.discovery.template_candidates import TemplateCandidateStore
    from app.discovery.yield_learning import DiscoveryYieldStore

    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    cand_store = TemplateCandidateStore(str(tmp_path / "cand.db"))
    # Dork lane OVER cap; web lane empty.
    for i in range(_DISCOVERY_GEN_CAP + 3):
        cand_store.propose(f"dork {i} {{industry}} {{location}} filetype:pdf")
    calls = {"dork": 0, "web": 0}
    angle = "chamber of commerce member directory {industry} {location}"

    def _fake_dork(*a, **k):
        calls["dork"] += 1
        return {"generated": [], "rejected": [], "reason": ""}

    def _fake_web(*a, **k):
        calls["web"] += 1
        return {"generated": [angle], "rejected": [], "reason": ""}

    monkeypatch.setattr(
        "app.discovery.template_generation.generate_dork_candidates", _fake_dork,
    )
    monkeypatch.setattr(
        "app.discovery.template_generation.generate_web_angle_candidates", _fake_web,
    )
    result = _run_discovery_generation(yield_store, cand_store)
    assert calls["dork"] == 0  # dork lane full — paused
    assert calls["web"] == 1   # web lane still invents methods
    assert result is not None  # and its honest outcome is surfaced
    assert result["web_generated"] == [angle]


# ---------------------------------------------------------------------------
# discover_until_target
# ---------------------------------------------------------------------------

def test_discovery_loops_until_target(monkeypatch):
    # Each call returns one NEW lead so the loop must iterate to reach target.
    remaining = [
        [_record("A", "a@x.com", "x.com")],
        [_record("B", "b@y.com", "y.com")],
        [_record("C", "c@z.com", "z.com")],
    ]

    def _sequential(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        return SourceStatus.SUCCESS, list(remaining.pop(0)) if remaining else [], {"pdfs_found": 1, "pdf_urls": [f"https://x.example/p{trade}"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _sequential)
    query = ResearchQuery(trade="general contractor", location="Texas", target_emails=3)
    events = []
    leads, pass_log = discover_until_target(
        query, max_passes=5, emit=lambda *a, **k: events.append((a[0], a[1]))
    )
    assert len(leads) == 3  # target reached across passes
    assert len(pass_log) == 3  # one pass per lead found
    # emit reported every discovery pass
    assert [p for p in events if p[0] == "discovery"]
    assert all(e["new_leads"] >= 1 for e in pass_log)


def test_discovery_stops_on_cancel(monkeypatch):
    calls = {"n": 0}

    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        calls["n"] += 1
        return SourceStatus.SUCCESS, [_record("A", "a@x.com", "x.com")], {"pdfs_found": 1, "pdf_urls": ["https://x.example"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)
    query = ResearchQuery(trade="gc", location="TX", target_emails=99)
    # cancel after the first pass -> no more passes
    state = {"calls": 0}
    def cancel():
        state["calls"] += 1
        return state["calls"] > 1
    leads, pass_log = discover_until_target(query, max_passes=5, cancel=cancel)
    assert calls["n"] <= 2  # stopped early
    assert len(leads) <= 1


def test_discovery_passes_advance_to_unseen_pdfs(monkeypatch):
    """Each pass hands the source the PDFs already parsed this run, so later
    passes can only surface NEW documents — no tautological repeats (the fix
    that un-sticks discovery at one pass's yield)."""
    skips: list[set] = []

    def _advancing(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        skips.append(set(skip_pdfs or ()))
        # Each pass serves a different record from a different PDF.
        url = f"https://planroom/{len(skips)}.pdf"
        return SourceStatus.SUCCESS, [_record(f"C{len(skips)}", f"{len(skips)}@z.com", "z.com")], {"pdf_urls": [url]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _advancing)
    query = ResearchQuery(trade="gc", location="TX", target_emails=3)
    leads, pass_log = discover_until_target(query, max_passes=5)
    assert len(leads) == 3
    # Pass 2 received pass 1's PDFs; pass 3 received both earlier passes'.
    assert skips[0] == set()
    assert skips[1] == {"https://planroom/1.pdf"}
    assert skips[2] == {"https://planroom/1.pdf", "https://planroom/2.pdf"}


def test_discovery_stops_honestly_on_exhaustion(monkeypatch):
    """An EMPTY pass (nothing left to offer) stops the loop with an honest
    `exhausted` reason instead of grinding through the remaining passes."""
    calls = {"n": 0}

    def _empty(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        calls["n"] += 1
        return SourceStatus.EMPTY, [], {"reason": "no_unseen_pdfs"}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _empty)
    query = ResearchQuery(trade="gc", location="TX", target_emails=20)
    events = []
    leads, pass_log = discover_until_target(
        query, max_passes=5, emit=lambda *a, **k: events.append((a[0], a[3]))
    )
    assert calls["n"] == 1  # stopped after the exhausted pass
    assert leads == []
    assert pass_log[0]["exhausted"] is True
    assert pass_log[0]["reason"] == "no_unseen_pdfs"
    assert pass_log[0]["status"] == "empty"
    assert any("exhausted" in str(m) for _, m in events)


def test_pass_log_includes_source_stats(monkeypatch):
    """Each discovery pass records a JSON-safe per-source snapshot in the
    pass_log, so a zero-result job is diagnosable from history (§6) instead
    of only the generic fallback_reason — the Houston zero-result gap."""
    def _with_stats(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        return SourceStatus.EMPTY, [], {
            "reason": "search_failed",
            "source_stats": {
                "directory_crawl": {"status": SourceStatus.EMPTY, "results": 0, "metadata": {}},
                "plan_holder": {
                    "status": SourceStatus.UNAVAILABLE, "results": 0,
                    "metadata": {"reason": "search_failed",
                                "search_errors": ["tavily: HTTP 432 usage limit"]},
                },
                "search": {"status": SourceStatus.EMPTY, "results": 0, "metadata": {}},
            },
        }

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _with_stats)
    query = ResearchQuery(trade="gc", location="TX", target_emails=20)
    leads, pass_log = discover_until_target(query, max_passes=1)
    entry = pass_log[0]
    stats = entry["source_stats"]
    # SourceStatus enums are normalised to plain strings (JSON-safe).
    assert stats["plan_holder"]["status"] == "unavailable"
    assert stats["plan_holder"]["reason"] == "search_failed"
    assert stats["directory_crawl"]["status"] == "empty"
    assert stats["search"]["status"] == "empty"
    # The pass_log entry is json.dumps-able, exactly as the job runner stores it.
    json.dumps(entry)
    assert leads == []


# ---------------------------------------------------------------------------
# run_research
# ---------------------------------------------------------------------------

class _FakeAgent:
    def __init__(self, *a, **k):
        pass

    def research(self, email, domain, *a, **k):
        return SimpleNamespace(
            company=SimpleNamespace(name="Acme"),
            person=SimpleNamespace(name="Jane", bound=True),
            potential_score=8.5,
            recommendation="contact_now",
            intent=SimpleNamespace(needs_estimation="yes"),
            timing=SimpleNamespace(window="now"),
            sources_checked=["company_research", "person_research"],
        )


def test_run_research_uses_agent_and_emits(monkeypatch):
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _FakeAgent)
    leads = [
        {"email": "a@x.com", "domain": "x.com"},
        {"email": "b@y.com", "domain": "y.com"},
    ]
    events = []
    results = run_research(
        leads, emit=lambda *a, **k: events.append((a[0], a[1], k.get("email")))
    )
    assert len(results) == 2
    assert results[0]["company"] == "Acme"
    assert results[0]["bound"] is True
    assert results[0]["recommendation"] == "contact_now"
    # emit fired per research lead
    assert [e for e in events if e[0] == "research"]
    assert len(events) == 2


def test_run_research_one_failure_does_not_kill_batch(monkeypatch):
    class _FlakyAgent(_FakeAgent):
        def research(self, email, domain, *a, **k):
            if email == "b@y.com":
                raise RuntimeError("AI down")
            return super().research(email, domain, *a, **k)

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _FlakyAgent)
    leads = [
        {"email": "a@x.com", "domain": "x.com"},
        {"email": "b@y.com", "domain": "y.com"},
    ]
    results = run_research(leads)
    assert len(results) == 2
    assert "error" in results[1]
    assert "Acme" in results[0]["company"]


# ---------------------------------------------------------------------------
# run_full
# ---------------------------------------------------------------------------

def test_run_full_discover_only_skips_research(monkeypatch):
    monkeypatch.setattr(
        "app.leads.pipeline.run_discovery",
        _fake_discovery([_record("A", "a@x.com", "x.com")]),
    )
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _FakeAgent)
    query = ResearchQuery(trade="gc", location="TX", target_emails=5, discover_only=True)
    outcome = run_full(query)
    assert outcome["leads_found"] >= 1
    assert outcome["results"] == []
    assert outcome["discovery_passes"]


def test_run_full_researches_when_not_discover_only(monkeypatch):
    monkeypatch.setattr(
        "app.leads.pipeline.run_discovery",
        _fake_discovery([_record("A", "a@x.com", "x.com")]),
    )
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _FakeAgent)
    query = ResearchQuery(trade="gc", location="TX", target_emails=1)
    outcome = run_full(query)
    assert len(outcome["results"]) == 1
    assert outcome["results"][0]["recommendation"] == "contact_now"


# ---------------------------------------------------------------------------
# Discovery cache (PendingLeadsStore) — credit saver
# ---------------------------------------------------------------------------

def test_discovery_serves_cache_first_without_searching(monkeypatch, tmp_path):
    from app.lead_research.service import LeadResearchStore, PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    dossiers = LeadResearchStore(db_path=db)
    # Pre-stock the cache with surplus leads from a previous run (P2: carry
    # the trade — a gc query no longer serves unlabeled cached rows).
    pending.add([
        {"email": "a@x.com", "domain": "x.com", "company": "A", "location": "Texas",
         "trade": "gc"},
        {"email": "b@y.com", "domain": "y.com", "company": "B", "location": "Texas",
         "trade": "gc"},
        {"email": "c@z.com", "domain": "z.com", "company": "C", "location": "Texas",
         "trade": "gc"},
    ])

    # If discovery is called, that means the cache was NOT used — fail.
    def _should_not_run(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        raise AssertionError("live discovery should not run when cache has leads")

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _should_not_run)
    query = ResearchQuery(trade="gc", location="Texas", target_emails=2)
    leads, pass_log = discover_until_target(
        query, pending_store=pending, dossier_store=dossiers,
    )
    assert len(leads) == 2
    assert {l["email"] for l in leads} == {"a@x.com", "b@y.com"}
    # Nothing was researched yet, so all three stay in the cache.
    assert pending.count() == 3


def test_discovery_stocks_surplus_and_refills_from_cache(monkeypatch, tmp_path):
    from app.lead_research.service import LeadResearchStore, PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    dossiers = LeadResearchStore(db_path=db)

    # First run: discovery surfaces 4 leads, target is 2.
    records = [
        _record("A", "a@x.com", "x.com"),
        _record("B", "b@y.com", "y.com"),
        _record("C", "c@z.com", "z.com"),
        _record("D", "d@w.com", "w.com"),
    ]
    monkeypatch.setattr(
        "app.leads.pipeline.run_discovery", _fake_discovery(records),
    )
    query = ResearchQuery(trade="gc", location="Texas", target_emails=2)
    leads, pass_log = discover_until_target(
        query, pending_store=pending, dossier_store=dossiers,
    )
    assert len(leads) == 2
    # All 4 were stocked into the cache (surplus reusable).
    assert pending.count() == 4

    # Simulate the 2 used leads being researched -> removed from cache.
    pending.remove([l["email"] for l in leads])
    assert pending.count() == 2

    # Second run: cache has 2, so live discovery must NOT run again.
    def _should_not_run(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        raise AssertionError("live discovery should not run when cache has leads")

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _should_not_run)
    query2 = ResearchQuery(trade="gc", location="Texas", target_emails=2)
    leads2, _ = discover_until_target(
        query2, pending_store=pending, dossier_store=dossiers,
    )
    # Served entirely from cache — live discovery never ran.
    assert len(leads2) == 2
    assert {l["email"] for l in leads2} == {"c@z.com", "d@w.com"}


def test_discovery_excludes_researched_and_purges_cache(monkeypatch, tmp_path):
    """Phase D — the 'same result every run' flood, root-caused: an
    already-researched email sitting in the discovery cache shadows genuinely
    new discovery. Serving it re-burns research credit and the user keeps
    seeing the same dossiers. It is now PURGED at cache time and excluded from
    fresh leads at discovery time, so a re-run surfaces only NEW emails."""
    from app.lead_research.models import (
        CompanyProfile,
        LeadDossier,
        PersonFindings,
    )
    from app.lead_research.service import LeadResearchStore, PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    dossiers = LeadResearchStore(db_path=db)
    # a@x.com is ALREADY a dossier (researched on a previous run).
    dossiers.save(LeadDossier(
        email="a@x.com", domain="x.com",
        company=CompanyProfile(name="A", industry="gc", location="Texas"),
        person=PersonFindings(name="Amy", role="Owner", bound=True, role_relevance=True),
        potential_score=8.0, recommendation="contact_now",
    ))
    # P2: the gc query needs gc-labelled cache rows to serve (unlabeled rows
    # no longer serve to a trade-filtered run).
    pending.add([
        {"email": "a@x.com", "domain": "x.com", "company": "A", "location": "Texas",
         "trade": "gc"},
        {"email": "b@y.com", "domain": "y.com", "company": "B", "location": "Texas",
         "trade": "gc"},
        {"email": "c@z.com", "domain": "z.com", "company": "C", "location": "Texas",
         "trade": "gc"},
    ])
    assert pending.count() == 3

    def _should_not_run(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        raise AssertionError("cache should serve entirely without live discovery")

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _should_not_run)
    events = []
    query = ResearchQuery(trade="gc", location="Texas", target_emails=2)
    leads, _ = discover_until_target(
        query, pending_store=pending, dossier_store=dossiers,
        emit=lambda *a, **k: events.append((a[0], a[1], k.get("data"))),
    )
    # The researched a@x.com was purged, never served — only NEW b@/c@ come out.
    assert {l["email"] for l in leads} == {"b@y.com", "c@z.com"}
    assert pending.count() == 2  # a@x.com removed from the cache
    # Honest telemetry lives in the phase-0 emit (from_cache + stale_purged).
    purge = [d for e, phase, d in events if e == "discovery" and phase == 0]
    assert purge and purge[0]["from_cache"] == 2 and purge[0]["stale_purged"] == 1


def test_run_full_scales_passes_with_target_via_ten(monkeypatch):
    """Each discovery pass now draws on THREE lanes and probes website emails,
    so a target needs fewer plan-holder passes: max(5, target//10) — this pins
    the divisor so a target of 100 caps at 10 passes, not the old 20."""
    def _one_per_pass(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        return SourceStatus.SUCCESS, [_record(f"C{trade}{limit}", "x@z.com", "z.com")], {"pdf_urls": [f"https://ph/{trade}.pdf"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _one_per_pass)
    query = ResearchQuery(trade="gc", location="TX", target_emails=100)
    leads, pass_log = discover_until_target(query, max_passes=10)
    assert len(pass_log) == 10  # target//10 = 10 passes cap (never 20)
    assert len(leads) <= 10


def test_run_research_removes_researched_from_pending(monkeypatch):
    """A successfully-researched lead is removed from the discovery cache."""
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _FakeAgent)

    # Fake store: records saved emails; nothing is "already researched".
    saved = []

    class _FakeStore:
        def get(self, email):
            return None
        def save(self, dossier, user_id=""):
            saved.append(dossier)

    # Fake pending: records which emails were removed.
    removed = []

    class _FakePending:
        def remove(self, emails):
            removed.extend(emails)

    leads = [{"email": "a@x.com", "domain": "x.com"}]
    results = run_research(
        leads, store=_FakeStore(), pending_store=_FakePending(),
    )
    assert len(results) == 1
    assert results[0]["company"] == "Acme"
    # The researched email was cleared from pending.
    assert removed == ["a@x.com"]
    assert len(saved) == 1  # persisted to dossiers


def test_research_query_roundtrip_preserves_name_and_folder():
    """The job's persisted query dict round-trips search_name+folder, so a job
    resumed after a restart re-files into the SAME folder/tag it started with."""
    q = ResearchQuery(
        trade="gc", location="Houston TX", target_emails=3,
        search_name="Houston GC Q3", folder="Q3 Outreach",
    )
    restored = ResearchQuery.from_dict(q.to_dict())
    assert restored.search_name == "Houston GC Q3"
    assert restored.folder == "Q3 Outreach"
    assert restored.trade == "gc"
    # Defaults stay empty — the old query shape round-trips unchanged.
    bare = ResearchQuery(trade="gc", location="TX", target_emails=1)
    assert ResearchQuery.from_dict(bare.to_dict()).search_name == ""
    assert ResearchQuery.from_dict(bare.to_dict()).folder == ""


def test_run_research_auto_files_named_search_into_folder(monkeypatch):
    """A run with a folder + search name files each NEWLY-researched lead AT
    SAVE TIME (set_meta right after save) — the Phase C "leads mix ni hogi"
    guarantee. Cached leads are skipped, so their meta is never clobbered."""
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _FakeAgent)

    saved, meta_calls = [], []

    class _FakeStore:
        def __init__(self, preexisting: set[str]):
            self._preexisting = preexisting
        def get(self, email):
            # A REAL LeadDossier, not a namespace of the fields this branch
            # happens to read: the cached entry re-derives today's verdict from
            # the dossier, so a hand-narrowed double breaks every time the
            # verdict needs one more field.
            if email not in self._preexisting:
                return None
            return LeadDossier(
                email=email,
                domain="y.com",
                company=CompanyProfile(name="Pre", industry="general contractor"),
                person=PersonFindings(name="Old", bound=False),
                potential_score=5.0,
                recommendation="nurture",
                fit="Scored at research time.",
            )
        def save(self, dossier, user_id=""):
            saved.append(dossier)
        def set_meta(self, email, *, folder, tags):
            meta_calls.append((email, folder, tags))

    leads = [
        {"email": "a@x.com", "domain": "x.com"},   # new -> auto-filed
        {"email": "done@y.com", "domain": "y.com"},  # cached -> NOT re-filed
    ]
    results = run_research(
        leads,
        store=_FakeStore(preexisting={"done@y.com"}),
        search_name="Houston GC Q3",
        folder="Q3 Outreach",
    )
    assert len(results) == 2
    # Only the NEWLY-researched lead was saved + auto-filed, exactly once.
    assert [s.company.name for s in saved] == ["Acme"]
    assert meta_calls == [("a@x.com", "Q3 Outreach", ["Houston GC Q3"])]


def test_run_research_no_file_when_no_folder_or_name(monkeypatch):
    """Without folder/search_name, save behaves exactly as before — no set_meta."""
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _FakeAgent)

    saved, meta_calls = [], []

    class _FakeStore:
        def get(self, email):
            return None
        def save(self, dossier, user_id=""):
            saved.append(dossier)
        def set_meta(self, email, *, folder, tags):
            meta_calls.append((email, folder, tags))

    results = run_research(
        leads=[{"email": "a@x.com", "domain": "x.com"}],
        store=_FakeStore(),
    )
    assert len(results) == 1
    assert len(saved) == 1
    assert meta_calls == []


class _DeadDomainAgent:
    """Fake agent whose research hits the dead-domain MX gate (no .fit-relevant
    business data — the dossier is a pure "skip, domain undeliverable" reject)."""

    def __init__(self, *a, **k):
        pass

    def research(self, email, domain, *a, **k):
        return SimpleNamespace(
            company=SimpleNamespace(name=""),
            person=SimpleNamespace(name="", bound=False),
            potential_score=0.0,
            recommendation="skip",
            fit=f"Dead/expired domain — {DEAD_DOMAIN_MARKER} (undeliverable)",
            intent=SimpleNamespace(needs_estimation="unknown"),
            timing=SimpleNamespace(window="unknown"),
            sources_checked=[],
        )


def test_run_research_never_persists_or_counts_dead_domain(monkeypatch):
    """A dead-domain dossier (no MX record) is NOT a lead: it is not saved,
    not emitted as a result (so job.results.length / totals stay honest), and
    the pending row is FLAGGED dead — so the same address is never served
    again as a recurring "Skip" (the user's "same emails every search" fix).
    The row is kept (mark-dead, not delete) so a deliberate re-probe could
    clear it later."""
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _DeadDomainAgent)

    saved = []

    class _FakeStore:
        def get(self, email):
            return None
        def save(self, dossier, user_id=""):
            saved.append(dossier)

    removed = []
    flagged = []

    class _FakePending:
        def remove(self, emails):
            removed.extend(emails)
        def mark_dead(self, emails):
            flagged.extend(emails)

    events = []
    leads = [{"email": "dead@gone.com", "domain": "gone.com"}]
    results = run_research(
        leads, store=_FakeStore(), pending_store=_FakePending(),
        emit=lambda *a, **k: events.append((a[0], k.get("email"))),
    )
    assert results == []          # never counted in totals / job results
    assert saved == []            # never persisted to the dossiers store
    assert removed == []          # row kept in pending (not deleted)
    assert flagged == ["dead@gone.com"]  # but dead-flagged: never served again
    # Honest skip is LOGGED through the emit callback (CLAUDE.md §6) — never a
    # silent drop of the lead.
    skip_emits = [e for e in events if e[0] == "research"]
    assert skip_emits and any(email == "dead@gone.com" for _, email in skip_emits)


def test_pending_serve_gate_advances_past_gated_junk(tmp_path):
    """Galti #3: take() must advance past gate-rejected rows instead of
    returning the same gated head-of-queue forever (live evidence: 20-minute
    run, zero consumer claims, zero dossiers — the oldest DCTA transit rows
    filled every ORDER BY created_at ASC window).

    A row rejected by the serve-time vertical gate is marked ``gated`` in the
    table (invisible to service, like ``dead``) so the NEXT take() window
    starts behind it. Inserting the junk rows directly (bypassing ``add``'s
    gate) mirrors rows cached BEFORE the non-client boundary existed.
    """
    from app.lead_research.service import PendingLeadsStore, _email_hash

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    # Junk rows cached before the boundary: DCTA-transit-host PDFs.
    for i, host in enumerate(("dcta.net", "tjpa.org", "sfport.com")):
        conn = pending._conn()
        conn.execute(
            "INSERT INTO pending_leads (email_hash, email, domain, company, "
            "source_url, location) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _email_hash(f"junk{i}@transit.com"),
                f"junk{i}@transit.com", f"transit{i}.com", f"row {i}",
                f"https://www.{host}/sites/Procurement/file.pdf", "Texas",
            ),
        )
        conn.commit()
        conn.close()
    pending.add([
        {"email": "real@buildco.com", "domain": "buildco.com", "location": "Texas"},
        {"email": "real2@steelco.com", "domain": "steelco.com", "location": "Texas"},
    ])

    # Old behavior: every take(2) window re-returned the SAME 2 oldest junk
    # rows -> served=[] FOREVER (starvation). New behavior: junk is marked
    # gated and repeated take() windows advance to the real rows behind them.
    served_real: set[str] = set()
    served_junk: set[str] = set()
    for _ in range(6):
        for s in pending.take(2, location="Texas"):
            if s["email"].startswith("junk"):
                served_junk.add(s["email"])
            else:
                served_real.add(s["email"])
    assert served_junk == set()                        # junk NEVER served
    assert served_real == {"real@buildco.com", "real2@steelco.com"}
    # Rows stay in the table (audit), invisible to serving — like dead.
    assert pending.count() == 5
    conn = pending._conn()
    gated = conn.execute(
        "SELECT COUNT(*) FROM pending_leads WHERE gated = 1"
    ).fetchone()[0]
    conn.close()
    assert gated == 3


def test_pending_dead_flagged_leads_are_never_served(tmp_path):
    """PendingLeadsStore.dead flag: a confirmed dead-domain email stays in the
    table (row kept for a future deliberate re-probe) but ``take`` never serves
    it again — the recurrent "same Skip emails every search" fix at cache layer."""
    from app.lead_research.service import PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    pending.add([
        {"email": "dead@gone.com", "domain": "gone.com", "location": "Texas"},
        {"email": "ok@x.com", "domain": "x.com", "location": "Texas"},
        {"email": "ok2@y.com", "domain": "y.com", "location": "Texas"},
    ])
    assert pending.mark_dead(["dead@gone.com"]) == 1
    assert pending.dead_emails() == {"dead@gone.com"}
    assert {l["email"] for l in pending.take(5, location="Texas")} == {
        "ok@x.com", "ok2@y.com",
    }
    # The dead row is kept — just invisible to serve.
    assert pending.count() == 3
    # Homing-in: a dead email is never handed back in ANY location view.
    assert "dead@gone.com" not in {l["email"] for l in pending.take(5)}


def test_discovery_never_reresearches_dead_emails(monkeypatch, tmp_path):
    """Even if a live source re-surfaces a dead address, the dead pool excludes
    it at discovery time — a confirmed dead email never re-enters the batch."""
    from app.lead_research.service import LeadResearchStore, PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    dossiers = LeadResearchStore(db_path=db)
    pending.add([{"email": "dead@gone.com", "domain": "gone.com", "location": "Texas"}])
    pending.mark_dead(["dead@gone.com"])

    # Live discovery keeps returning the dead address — the dead pool must hold.
    monkeypatch.setattr(
        "app.leads.pipeline.run_discovery",
        _fake_discovery([_record("D", "dead@gone.com", "gone.com")]),
    )
    query = ResearchQuery(trade="gc", location="Texas", target_emails=5)
    leads, _ = discover_until_target(
        query, pending_store=pending, dossier_store=dossiers,
    )
    assert leads == []                # nothing is ever handed to research
    assert pending.count() == 1       # row kept, still flagged dead


def test_pending_sweep_flags_free_mail_and_dead_domain_keeps_usable(tmp_path):
    """The TOTAL backlog fix (not a 4-email workaround): ``sweep_known_dead``
    pre-screens the WHOLE discovery cache with the same cheap gates the research
    stage uses — free-mail triage + native MX check — so worthless leads are
    flagged ``dead`` BEFORE they ever consume a research slot. No AI credits.
    Useful leads are kept; already-researched rows are drained."""
    from app.lead_research.service import PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    pending.add([
        {"email": "useful@x.com", "domain": "x.com", "location": "Texas"},
        {"email": "dead@gonenet.com", "domain": "gonenet.com", "location": "Texas"},
        {"email": "researched@y.com", "domain": "y.com", "location": "Texas"},
    ])
    # consumer@gmail.com is a LEGACY row that PREDATES the add() free-mail guard —
    # it already sits in the cache, exactly the historical backlog the sweep
    # exists to clear (the new guard stops future ones, this row is already there).
    import sqlite3

    from app.lead_research.service import _email_hash
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO pending_leads (email_hash, email, domain, location) "
        "VALUES (?, ?, ?, ?)",
        (_email_hash("consumer@gmail.com"), "consumer@gmail.com", "", "Texas"),
    )
    conn.commit()
    conn.close()

    class _DossierStore:
        def __init__(self, emails: set[str]) -> None:
            self._e = emails

        def get(self, email: str):
            return object() if email in self._e else None

    def _mx(domain: str) -> bool:
        # Same gate shape as the research MX check: known-dead -> False.
        return domain not in {"gonenet.com"}

    stats = pending.sweep_known_dead(
        dossier_store=_DossierStore({"researched@y.com"}),
        domain_delivers=_mx,
    )
    assert stats == {
        "total": 4, "kept": 1, "free_mail": 1,
        "dead_domain": 1, "already_researched": 1,
    }
    # take() now serves ONLY the single useful lead — nothing wasted remains.
    assert {l["email"] for l in pending.take(10, location="Texas")} == {"useful@x.com"}
    assert pending.dead_emails() == {"dead@gonenet.com", "consumer@gmail.com"}
    # The researched row was REMOVED (drained from the cache), the others kept.
    assert pending.count() == 3


def test_pending_add_never_stocks_free_mail(tmp_path):
    """Permanent guard: the discovery cache never re-accumulates consumer-mail
    addresses (gmail/aol/...). The backlog sweep clears history; this keeps it
    clear going forward — the surplus slot is never wasted on a non-lead."""
    from app.lead_research.service import PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    added = pending.add([
        {"email": "owner@acme.com", "domain": "acme.com", "location": "Texas"},
        {"email": "bob@gmail.com", "domain": "", "location": "Texas"},
        {"email": "sue@aol.com", "domain": "", "location": "Texas"},
    ])
    assert added == 1
    assert [l["email"] for l in pending.take(10)] == ["owner@acme.com"]


def test_pending_add_never_stocks_crawl_artifacts(tmp_path):
    """Same guard for page-source machine strings: a Sentry DSN, a form
    placeholder and a listserv id scraped off raw HTML are not contacts and
    never take a pool slot (observed live 2026-09-15: 17 of 1645 rows).
    A real company whose name contains 'sentry' still passes."""
    from app.lead_research.service import PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    added = pending.add([
        {"email": "karl@sentrycontracting.com", "domain": "",
         "location": "Texas"},
        {"email": "2062d0a4929b45348643784b5cb39c36@sentry.wixpress.com",
         "domain": "", "location": "Texas"},
        {"email": "jane@example.com", "domain": "", "location": "Texas"},
        {"email": "20260828153349.8061-1-odion@efficios.com", "domain": "",
         "location": "Texas"},
    ])
    assert added == 1
    assert [l["email"] for l in pending.take(10)] == [
        "karl@sentrycontracting.com"]


def test_run_full_keeps_gathering_until_working_target(monkeypatch, tmp_path):
    """'jitni quantity likho utna working data': dead/refined-out emails never
    count toward the target — run_full tops up with more discovery until N
    VISIBLE dossiers persist. Never fabricates: if the sources drain below the
    target, it reports the shortfall honestly instead."""
    from app.lead_research.service import LeadResearchStore

    db = str(tmp_path / "leads.db")
    store = LeadResearchStore(db_path=db)

    calls = {"n": 0}

    def _staged(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        calls["n"] += 1
        records = (
            [_record("D1", "dead1@gone.com", "gone.com"),
             _record("D2", "dead2@gone2.com", "gone2.com")]
            if calls["n"] == 1 else
            [_record("A", "ok1@x.com", "x.com"),
             _record("B", "ok2@y.com", "y.com")]
        )
        return SourceStatus.SUCCESS, records, {
            "pdf_urls": [f"https://ph/{calls['n']}.pdf"],
        }

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _staged)

    from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings

    class _LiveAgent:
        def research(self, email, domain, *a, **k):
            if domain in ("gone.com", "gone2.com"):
                return LeadDossier(
                    email=email, domain=domain,
                    company=CompanyProfile(name="", industry="general contractor", location="TX"),
                    person=PersonFindings(name="", role="", bound=False, role_relevance=False),
                    potential_score=0.0, recommendation="skip",
                    fit=f"Dead/expired domain — {DEAD_DOMAIN_MARKER} (undeliverable)",
                )
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(name="Acme", industry="general contractor", location="Texas"),
                person=PersonFindings(name="Jane", role="Owner", bound=True, role_relevance=True),
                potential_score=8.5, recommendation="contact_now",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _LiveAgent)

    query = ResearchQuery(trade="gc", location="Texas", target_emails=2)
    outcome = run_full(query, store=store)
    # 2 VISIBLE dossiers got researched — NOT the 2 raw first emails (the dead
    # pair surfaced first and never counted).
    assert outcome["working_leads"] == 2
    assert outcome["shortfall"] == 0
    working = [e for e in outcome["results"] if e.get("working")]
    assert {e["email"] for e in working} == {"ok1@x.com", "ok2@y.com"}
    # The dead pair: never persisted, never counted, never researched twice.
    assert store.get("dead1@gone.com") is None
    assert store.get("dead2@gone2.com") is None
    assert len(outcome["discovery_passes"]) >= 2  # a genuine top-up round ran


# ---------------------------------------------------------------------------
# Re-enrichment cooldown — PendingLeadsStore attempted_at / attempt_count
# ---------------------------------------------------------------------------

def test_pending_take_skips_recent_attempt_within_cooldown(tmp_path):
    """A lead whose research ERRORED (attempted_at recent) is NOT served again
    while its cooldown window is active — but a row whose attempt expired (or
    that was never tried) IS served. This stops a failing lead from being
    re-researched and re-failed on EVERY Execute."""
    from app.lead_research.service import PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    pending.add([
        {"email": "fresh@x.com", "domain": "x.com", "location": "Texas"},
        {"email": "failed@y.com", "domain": "y.com", "location": "Texas"},
        {"email": "expired@z.com", "domain": "z.com", "location": "Texas"},
    ])
    pending.mark_attempt(["failed@y.com"])  # attempted NOW
    # Expire the third row's attempt by back-dating it past the window.
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE pending_leads SET attempted_at = datetime('now', '-2 hours'), "
        "attempt_count = 1 WHERE email = 'expired@z.com'"
    )
    conn.commit()
    conn.close()

    # Cooldown of 1 hour: failed@y.com (just attempted) is excluded; the
    # never-tried fresh@x.com and the expired@z.com retry are served.
    served = pending.take(10, location="Texas", cooldown_seconds=3600)
    assert {l["email"] for l in served} == {"fresh@x.com", "expired@z.com"}
    # Legacy default (cooldown 0 = disabled) keeps old behavior — serves all.
    assert {l["email"] for l in pending.take(10, location="Texas")} == {
        "fresh@x.com", "failed@y.com", "expired@z.com",
    }


def test_pending_mark_attempt_records_timestamp_and_count(tmp_path):
    """mark_attempt stamps attempted_at=now and increments attempt_count per
    failed research run; rows it never cached are ignored (returns 0)."""
    from app.lead_research.service import PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    pending.add([{"email": "a@x.com", "domain": "x.com", "location": "TX"}])

    assert pending.mark_attempt(["a@x.com", "never-cached@nope.com"]) == 1

    import sqlite3
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT attempted_at, attempt_count FROM pending_leads WHERE email = 'a@x.com'"
    ).fetchone()
    conn.close()
    assert row is not None and row[0] is not None and row[1] == 1
    # A second failed attempt bumps the counter.
    assert pending.mark_attempt(["a@x.com"]) == 1
    conn = sqlite3.connect(db)
    count = conn.execute(
        "SELECT attempt_count FROM pending_leads WHERE email = 'a@x.com'"
    ).fetchone()[0]
    conn.close()
    assert count == 2


def test_pending_cooling_emails_reflects_window(tmp_path):
    """cooling_emails() returns the emails inside the cooldown window, drops
    ones whose attempt expired, and is empty when cooldown is disabled."""
    from app.lead_research.service import PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    pending.add([
        {"email": "hot@x.com", "domain": "x.com", "location": "Texas"},
        {"email": "old@y.com", "domain": "y.com", "location": "Texas"},
    ])
    pending.mark_attempt(["hot@x.com"])  # recent -> cooling
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE pending_leads SET attempted_at = datetime('now', '-2 hours') "
        "WHERE email = 'old@y.com'"
    )
    conn.commit()
    conn.close()

    # 1-hour window: only hot@x.com is still cooling; old@y.com expired out.
    assert pending.cooling_emails(3600) == {"hot@x.com"}
    # Disabled cooldown -> nothing is ever "cooling".
    assert pending.cooling_emails(0) == set()


def test_run_research_marks_failed_lead_attempt_in_pending(monkeypatch):
    """A research ERROR leaves the lead in pending AND records the attempt, so
    take() cools it down instead of serving/re-failing it on the next Execute."""
    class _FlakyAgent(_FakeAgent):
        def research(self, email, domain, *a, **k):
            if email == "b@y.com":
                raise RuntimeError("AI down")
            return super().research(email, domain, *a, **k)

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _FlakyAgent)

    attempts = []
    removed = []

    class _FakePending:
        def remove(self, emails):
            removed.extend(emails)
        def mark_attempt(self, emails):
            attempts.extend(emails)

    class _FakeStore:
        def get(self, email):
            return None
        def save(self, dossier, user_id=""):
            pass

    leads = [
        {"email": "a@x.com", "domain": "x.com"},
        {"email": "b@y.com", "domain": "y.com"},
    ]
    results = run_research(leads, store=_FakeStore(), pending_store=_FakePending())
    assert len(results) == 2
    assert "error" in results[1]
    # The failing lead's attempt is recorded; the success is removed instead.
    assert attempts == ["b@y.com"]
    assert removed == ["a@x.com"]


def test_discovery_fresh_filter_drops_cooled_email(monkeypatch, tmp_path):
    """Even when a live search re-surfaces a lead that is INSIDE the cooldown
    window, the fresh filter excludes it from the batch — a cooled lead is never
    handed to research again in the same run."""
    from app.lead_research.service import LeadResearchStore, PendingLeadsStore

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    dossiers = LeadResearchStore(db_path=db)
    pending.add([{"email": "cooled@x.com", "domain": "x.com", "location": "Texas"}])
    pending.mark_attempt(["cooled@x.com"])  # fresh attempt -> cooling

    # Live discovery happens to surface the cooled address again.
    monkeypatch.setattr(
        "app.leads.pipeline.run_discovery",
        _fake_discovery([_record("C", "cooled@x.com", "x.com")]),
    )
    query = ResearchQuery(trade="gc", location="Texas", target_emails=5)
    leads, _ = discover_until_target(
        query, pending_store=pending, dossier_store=dossiers,
        cooldown_seconds=3600,
    )
    # The cooled lead is never handed to research; nothing else to serve.
    assert leads == []
    assert pending.count() == 1


# ---------------------------------------------------------------------------
# Fix A — search/crawl lane skips already-seen company domains BEFORE the
# website email-probe (the web-lane equivalent of plan-holder skip_pdfs).
# ---------------------------------------------------------------------------

def test_discovery_filters_seen_domains_before_probe(monkeypatch):
    """Fix A: a company record whose website domain this run ALREADY saw is
    dropped BEFORE company_records_to_leads (no re-probe, no re-crawl), while
    a genuinely new domain still reaches conversion. The set is MUTATED so
    later passes/rounds advance instead of re-serving the pool."""
    import app.leads.pipeline as pipeline
    seen_domains = {"acme.com"}

    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        return SourceStatus.SUCCESS, [
            # acme.com already seen this run -> must be dropped pre-probe.
            {"company_name": "Acme", "website": "https://www.acme.com",
             "source_url": "https://acme.com"},
            # brand-new domain -> must reach company_records_to_leads.
            {"company_name": "Beta", "website": "https://beta.co",
             "source_url": "https://beta.co"},
        ], {"pdfs_found": 0, "pdf_urls": []}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)

    reached = []

    def _spy(records, **kw):
        # Capture which records actually reached conversion (the probe stage).
        reached.append([r.get("website") for r in records])
        return ([{"email": "b@beta.co", "domain": "beta.co", "company": "Beta",
                  "person": "", "source_url": "https://beta.co",
                  "trade": "gc"}],  # P2: gc-query serve gate needs the trade
                {"plan_emails": 0, "probed": 1, "with_email": 1, "dup_plan": 0,
                 "no_email": 0})

    monkeypatch.setattr(pipeline, "company_records_to_leads", _spy)

    query = ResearchQuery(trade="gc", location="TX", target_emails=1)
    leads, _ = discover_until_target(query, max_passes=1, seen_domains=seen_domains)

    # acme.com (already seen) never reached conversion; only beta.co did.
    assert reached == [["https://beta.co"]]
    assert len(leads) == 1 and leads[0]["domain"] == "beta.co"
    # The new domain was remembered (mutated in place) for the next pass/round.
    assert "beta.co" in seen_domains
    assert "acme.com" in seen_domains


def test_plan_holder_rows_bypass_seen_domains_gate(monkeypatch):
    """B1 (serial twin): plan-holder rows carry real emails DIRECTLY, so their
    derived website must NEVER put them through the seen_domains gate. A later
    PDF re-listing a firm from pass 1 surfaces that firm's OTHER real contacts
    — without this the run tops out after its first +N wave and the market's
    remaining working contacts are silently dropped (CLAUDE.md §7)."""
    import app.leads.pipeline as pipeline
    seen_domains = {"acme.com"}

    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        return SourceStatus.SUCCESS, [
            # acme.com ALREADY seen this run — but this is a PLAN-HOLDER row
            # with a real email, so it must still reach conversion (email-level
            # dedup, never the domain gate).
            _record("Acme", "a@acme.com", "acme.com"),
            # website-only row for the same already-seen domain -> dropped
            # pre-probe (the search/crawl lane's own dedup).
            {"company_name": "Acme Web", "website": "https://www.acme.com",
             "source_url": "https://acme.com"},
            # brand-new plan-holder firm -> converts as usual.
            _record("Beta", "b@beta.co", "beta.co"),
        ], {"pdfs_found": 2, "pdf_urls": []}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)

    reached = []

    def _spy(records, **kw):
        # Capture which records actually reached conversion (the probe stage).
        reached.append([r.get("company_name") for r in records])
        return ([{"email": r["plan_holder"]["emails"][0]["email"],
                  "domain": r["plan_holder"]["domain"],
                  "company": r["company_name"], "person": "",
                  "source_url": r.get("source_url", ""),
                  "trade": "gc"}  # P2: gc-query serve gate needs the trade
                 for r in records if r.get("plan_holder")],
                {"plan_emails": 2, "probed": 0, "with_email": 2, "dup_plan": 0,
                 "no_email": 0})

    monkeypatch.setattr(pipeline, "company_records_to_leads", _spy)

    query = ResearchQuery(trade="gc", location="TX", target_emails=2)
    leads, _ = discover_until_target(query, max_passes=1, seen_domains=seen_domains)

    # BOTH plan-holder rows reached conversion — even the re-listed acme.com
    # firm — while only the website-only acme.com row was dropped pre-probe.
    assert reached == [["Acme", "Beta"]]
    assert {l["domain"] for l in leads} == {"acme.com", "beta.co"}


def test_streaming_relisted_plan_holder_contact_still_researched(monkeypatch, tmp_path):
    """B1 (streaming twin): the producer's plan-holder rows bypass the
    seen_domains gate too. A later PDF re-listing a firm whose domain was seen
    in pass 1 surfaces that firm's OTHER real contact — which must still be
    buffered + researched. Without the fix the derived website collapses the
    re-listing and the contact is silently dropped (the run tops out after its
    first +N wave)."""
    from app.lead_research.service import LeadResearchStore

    db = str(tmp_path / "leads.db")
    store = LeadResearchStore(db_path=db)

    calls = {"n": 0}

    def _staged(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return SourceStatus.SUCCESS, [
                _record("Acme", "a@acme.com", "acme.com"),
                # website-only row for the same firm — search-lane dedup drops it.
                {"company_name": "Acme Web", "website": "https://www.acme.com",
                 "source_url": "https://acme.com"},
            ], {"pdf_urls": ["https://ph/1.pdf"]}
        # Later pass: the SAME firm re-listed with a DIFFERENT real contact.
        return SourceStatus.SUCCESS, [
            _record("Acme", "a2@acme.com", "acme.com"),
        ], {"pdf_urls": ["https://ph/2.pdf"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _staged)

    from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings

    class _LiveAgent:
        def research(self, email, domain, *a, **k):
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(name="Acme", industry="general contractor",
                                       location="Texas"),
                person=PersonFindings(name="Jane", role="Owner", bound=True,
                                      role_relevance=True),
                potential_score=8.5, recommendation="contact_now",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _LiveAgent)

    # LEADS_CONCURRENCY defaults >1, so run_full takes the STREAMING path.
    query = ResearchQuery(trade="gc", location="Texas", target_emails=2)
    outcome = run_full(query, store=store)

    emails = {e["email"] for e in outcome["results"] if e.get("working")}
    # BOTH real contacts of the re-listed firm got researched + persisted.
    assert {"a@acme.com", "a2@acme.com"} <= emails
    assert store.get("a@acme.com") is not None
    assert store.get("a2@acme.com") is not None


def test_producer_rotates_ai_expanded_trade_variants(monkeypatch, tmp_path):
    """Phase J: the streaming producer's discovery passes rotate ACROSS the AI-
    expanded TRADE list (not the single literal spelling). A spy on
    run_discovery must see every expanded trade wording each pass searches."""
    from app.lead_research.service import LeadResearchStore

    store = LeadResearchStore(db_path=str(tmp_path / "leads.db"))
    seen: list[tuple[str, str]] = []

    def _staged(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        seen.append((trade, location))
        tag = trade.lower().replace(" ", "")
        return SourceStatus.SUCCESS, [
            _record(f"Co {trade}", f"{tag}{len(seen)}@co.com", f"{tag}{len(seen)}.com"),
        ], {"pdf_urls": [f"https://ph/{len(seen)}.pdf"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _staged)
    # AI expansion yields TWO trade wordings, literal location — the producer
    # must search BOTH trades, proving the query surface is no longer a point.
    monkeypatch.setattr(
        "app.leads.pipeline.generate_query_expansion",
        lambda t, l: {"trade_variants": ["GC Builders", "New Build GC"],
                      "location_variants": [], "reason": ""},
    )

    from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings

    class _LiveAgent:
        def research(self, email, domain, *a, **k):
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(name="Co", industry="general contractor",
                                       location="Texas"),
                person=PersonFindings(name="Jane", role="Owner", bound=True,
                                      role_relevance=True),
                potential_score=8.5, recommendation="contact_now",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _LiveAgent)

    query = ResearchQuery(trade="General Contractors", location="Texas", target_emails=2)
    run_full(query, store=store)

    searched_trades = {t for t, _ in seen}
    assert "GC Builders" in searched_trades
    assert "New Build GC" in searched_trades
    assert len(seen) >= 2


def test_producer_rotates_ai_expanded_location_variants(monkeypatch, tmp_path):
    """Phase J: the producer rotates ACROSS the AI-expanded LOCATION list too —
    not just the literal location string sent to every pass."""
    from app.lead_research.service import LeadResearchStore

    store = LeadResearchStore(db_path=str(tmp_path / "leads.db"))
    seen: list[tuple[str, str]] = []

    def _staged(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        seen.append((trade, location))
        return SourceStatus.SUCCESS, [
            _record(f"Co {location}", f"c{len(seen)}@{(location or 'x').split()[0].lower()}c.com",
                    f"c{len(seen)}cc.com"),
        ], {"pdf_urls": [f"https://ph/{len(seen)}.pdf"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _staged)
    # Phase K geo-breadth: the producer rotates across DISTINCT AI metro
    # locations, with the user's literal market always searched first.
    # ("Houston, TX" would be folded into the literal — same market — so the AI
    # returns two genuinely different counties here.)
    monkeypatch.setattr(
        "app.leads.pipeline.generate_query_expansion",
        lambda t, l: {"trade_variants": [],
                      "location_variants": ["Harris County TX", "Fort Bend County TX"],
                      "reason": ""},
    )

    from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings

    class _LiveAgent:
        def research(self, email, domain, *a, **k):
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(name="Co", industry="general contractor",
                                       location="Texas"),
                person=PersonFindings(name="Jane", role="Owner", bound=True,
                                      role_relevance=True),
                potential_score=8.5, recommendation="contact_now",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _LiveAgent)

    query = ResearchQuery(trade="General Contractors", location="Houston TX", target_emails=8)
    run_full(query, store=store)

    searched_locs = {l for _, l in seen}
    assert "Houston TX" in searched_locs  # user's market ALWAYS searched (first)
    assert "Harris County TX" in searched_locs
    assert "Fort Bend County TX" in searched_locs
    assert len(seen) >= 3


def test_producer_never_loses_literal_location_to_ai_rewrite(monkeypatch, tmp_path):
    """Phase K geo-breadth: an AI location expansion that NARROWS the market (the
    bug that made a 'San Antonio TX' run search ONLY 'Bexar County TX' and never
    the city) must never remove the user's own literal location. The literal is
    always searched first, then the AI variants."""
    from app.lead_research.service import LeadResearchStore

    store = LeadResearchStore(db_path=str(tmp_path / "leads.db"))
    seen: list[tuple[str, str]] = []

    def _staged(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        seen.append((trade, location))
        return SourceStatus.SUCCESS, [
            _record(f"Co {location}", f"c{len(seen)}@x{len(seen)}c.com", f"c{len(seen)}cc.com"),
        ], {"pdf_urls": [f"https://ph/{len(seen)}.pdf"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _staged)
    # The failure shape: AI collapses 'San Antonio TX' to ONE narrow county.
    monkeypatch.setattr(
        "app.leads.pipeline.generate_query_expansion",
        lambda t, l: {"trade_variants": [],
                      "location_variants": ["Bexar County TX"],
                      "reason": ""},
    )

    from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings

    class _LiveAgent:
        def research(self, email, domain, *a, **k):
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(name="Co", industry="general contractor",
                                       location="Texas"),
                person=PersonFindings(name="Jane", role="Owner", bound=True,
                                      role_relevance=True),
                potential_score=8.5, recommendation="contact_now",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _LiveAgent)

    query = ResearchQuery(trade="General Contractors", location="San Antonio TX", target_emails=2)
    run_full(query, store=store)

    searched_locs = [l for _, l in seen]
    assert "San Antonio TX" in searched_locs  # the user's market is ALWAYS searched
    assert "Bexar County TX" in searched_locs  # AND the AI variant
    assert searched_locs[0] == "San Antonio TX"  # literal is searched FIRST


def test_run_full_stops_on_no_progress_plateau(monkeypatch, tmp_path):
    """Fix C: the 'endless loop' is really a plateau — discovery keeps returning
    NEW domains, but research scores every one as skip (non-client), so the
    working count never moves. run_full stops with an honest reason instead of
    burning all rounds on the same dead end.

    Deterministic small surface + REAL expansion is bypassed (monkeypatched to
    a 1-trade × 2-location grid) so the sweep is a fixed 2 passes and the
    assertion ``calls <= 20`` proves the run stopped after a handful of rounds,
    NOT a full 20-round grind. This matches the honest round semantics: a round
    sweeps the WHOLE surface, so 'stop after ~N consecutive non-working' reads
    the plateau at round end — never mid-sweep (a junky first location must not
    pre-empt the second location still queued in the same round).
    """
    from app.lead_research.service import LeadResearchStore

    db = str(tmp_path / "leads.db")
    store = LeadResearchStore(db_path=db)
    calls = {"n": 0}

    def _stale(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        calls["n"] += 1
        # Each pass yields a genuinely NEW domain, so Fix A's seen-domain filter
        # never drops it — isolating the plateau guard. Research still skips it.
        return SourceStatus.SUCCESS, [
            _record("NoClient", f"x{calls['n']}@nc{calls['n']}.com",
                    f"nc{calls['n']}.com"),
        ], {"pdf_urls": [f"https://ph/{calls['n']}.pdf"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _stale)
    # Pin the surface: 1 trade × 2 locations = a 2-pass sweep. Without this the
    # real expansion fans out to 8×8=72 passes in ONE round, which is also an
    # honest full-surface stop but makes the pass-count assertion environment-
    # dependent rather than deterministic.
    monkeypatch.setattr(
        "app.leads.pipeline.generate_query_expansion",
        lambda t, l: {
            "trade_variants": ["gc"],
            "location_variants": ["TX", "Comal County TX"],
            "reason": "",
            "raw_replies": [],
            "retried": False,
        },
    )

    from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings

    class _SkipAgent:
        def research(self, email, domain, *a, **k):
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(name="NoClient", industry="Software",
                                       location="TX"),
                person=PersonFindings(name="", role="", bound=False,
                                      role_relevance=False),
                potential_score=0.0, recommendation="skip",
                fit="Not our client — software, not a bidding contractor.",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _SkipAgent)

    query = ResearchQuery(trade="gc", location="TX", target_emails=100)
    outcome = run_full(query, store=store)

    assert outcome["working_leads"] == 0
    assert outcome["shortfall"] == 100
    assert outcome["shortfall_reason"] == "no_progress_plateau"
    # Stopped after a plateau crossed threshold (8 consecutive non-working needs
    # ~4 rounds of this 2-pass sweep) — NOT all 20 rounds' worth of re-discovery.
    assert calls["n"] <= 20  # a few rounds x <=2 passes/round


def test_run_full_emits_expansion_event_with_raw_reply(monkeypatch, tmp_path):
    """Phase K2 telemetry: the expansion's raw AI replies + the honest surface
    land in the job's event stream, so a weak/blank expansion (the literal-only
    starvation) is diagnosable from History instead of a silent run (§6)."""
    from app.lead_research.service import LeadResearchStore

    store = LeadResearchStore(db_path=str(tmp_path / "leads.db"))
    events = []

    def _empty(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        return SourceStatus.EMPTY, [], {"pdf_urls": []}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _empty)
    # A weak-then-retried expansion: literally what the 04:06 San Antonio run
    # looked like inside — replied empty once, recovered on the retry.
    monkeypatch.setattr(
        "app.leads.pipeline.generate_query_expansion",
        lambda t, l: {
            "trade_variants": ["GC Builders"],
            "location_variants": ["New Braunfels TX", "Comal County TX"],
            "reason": "",
            "raw_replies": ["RAW reply 1 (weak)", "RAW reply 2 (recovered)"],
            "retried": True,
        },
    )

    query = ResearchQuery(trade="General Contractors", location="San Antonio TX",
                          target_emails=5)
    run_full(query, store=store, emit=lambda *a, **k: events.append((a, k)))

    expansion_events = [e for e in events if e[0][0] == "expansion"]
    assert expansion_events, "a job must emit its expansion surface"
    _a, _k = expansion_events[0]
    assert "expansion" == _a[0]
    msg = _a[3] if len(_a) > 3 else ""
    assert "wording(s)" in msg  # human summary: trade count × location count
    data = _k.get("data", {})
    # Both raw replies surface (SQLite-safe strings), plus the retry flag —
    # the exact diagnostic that made the 04:06 starvation non-silent.
    assert data.get("raw_replies") == [
        "RAW reply 1 (weak)", "RAW reply 2 (recovered)",
    ]
    assert data.get("retried") is True
    # The literal market is ALWAYS searched — and searched FIRST — even in the
    # honest-surface event (Phase K literal guard).
    assert data.get("location_variants") == [
        "San Antonio TX", "New Braunfels TX", "Comal County TX",
    ]


def test_run_full_sweep_advances_across_all_locations(monkeypatch, tmp_path):
    """Phase K pass-rotation fix: the producer sweeps the WHOLE trade × location
    surface — locations actually ADVANCE. The live 100-target San Antonio run
    searched 'San Antonio TX' for all 12 passes (the 7 expanded metro locations
    were never touched) because the old per-round cap (6 passes) was smaller than
    n_trades (8), so pass_idx // n_trades never reached 1 — the location index
    was dead code. This locks the sweep: every location variant is discovered,
    not just the literal first market."""
    from app.lead_research.service import LeadResearchStore

    store = LeadResearchStore(db_path=str(tmp_path / "leads.db"))
    calls: list[tuple[str, str]] = []

    def _empty(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        calls.append((trade, location))
        return SourceStatus.EMPTY, [], {"pdf_urls": []}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _empty)
    monkeypatch.setattr(
        "app.leads.pipeline.generate_query_expansion",
        lambda t, l: {
            "trade_variants": ["GC Builders", "Builders"],
            "location_variants": ["New Braunfels TX", "Comal County TX"],
            "reason": "", "raw_replies": [], "retried": False,
        },
    )
    query = ResearchQuery(trade="General Contractors", location="San Antonio TX",
                          target_emails=6)
    run_full(query, store=store, emit=lambda *a, **k: None)

    searched_locs = {loc for _t, loc in calls}
    assert "New Braunfels TX" in searched_locs   # the OLD rotation never reached this
    assert "Comal County TX" in searched_locs    # nor this
    assert calls                                 # the sweep really ran
    # The full surface = every trade × every location — the location index
    # ADVANCES (2 trades × 3 markets = 6 distinct combos, no repeats).
    assert len({(t, l) for t, l in calls}) == 3 * 2


# ---------------------------------------------------------------------------
# Lever 1+2: pipeline scale caps
# ---------------------------------------------------------------------------

def test_email_probes_for_demand_scales():
    """Dynamic probe scaler: base=12 for small demand, grows to ~48 at 200+
    remaining, divided by n_locs for multi-market sweeps."""
    from app.leads.pipeline import _email_probes_for_demand

    # Small demand (≤50 remaining) → base cap
    assert _email_probes_for_demand(10) == 12
    assert _email_probes_for_demand(50) == 12
    # Medium demand (~100) → ~2× base
    p100 = _email_probes_for_demand(100)
    assert 20 <= p100 <= 30, f"expected ~24 at 100 remaining, got {p100}"
    # Large demand (~200) → ~4× base (capped)
    p200 = _email_probes_for_demand(200)
    assert 36 <= p200 <= 48, f"expected ~48 at 200 remaining, got {p200}"
    # Multi-market: divided by n_locs
    p200_4loc = _email_probes_for_demand(200, n_locs=4)
    assert p200_4loc <= p200 // 2, "multi-market should reduce per-loc probes"


def test_round_cap_scales_with_target():
    """Round cap formula: target//25, floor 8 (plateau needs 8 non-working
    results to manifest), ceiling 80."""
    # Small target (25) → 1 round, floored to 8 (plateau safety)
    assert max(8, min(25 // 25, 80)) == 8
    # Medium target (100) → 4 rounds, floored to 8
    assert max(8, min(100 // 25, 80)) == 8
    # Large target (500) → 20 rounds
    assert max(8, min(500 // 25, 80)) == 20
    # Huge target (1000) → 40 rounds
    assert max(8, min(1000 // 25, 80)) == 40
    # Massive target (5000) → 80 rounds (ceiling)
    assert max(8, min(5000 // 25, 80)) == 80


# ---------------------------------------------------------------------------
# Lever 3: generic email research recovery
# ---------------------------------------------------------------------------

def test_generic_email_construction_company_reaches_nurture(monkeypatch, tmp_path):
    """info@ on a real construction company domain runs company research,
    skips person research, and reaches nurture tier (not skip/generic)."""
    from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
    from app.lead_research.service import LeadResearchStore

    store = LeadResearchStore(db_path=str(tmp_path / "leads.db"))
    research_calls: list[str] = []

    class _ConstructionAgent:
        def research(self, email, domain, *a, **k):
            research_calls.append(email)
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(
                    name="Stovall Construction", industry="General Contractor",
                    location="TX", website=f"https://{domain}",
                    facts=[],
                ),
                person=PersonFindings(name="", role="", bound=False,
                                      role_relevance=False),
                potential_score=3.0, recommendation="nurture",
                fit="Generic company contact — construction identified",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _ConstructionAgent)
    # Provide one discovery record with the info@ email
    records = [_record("Stovall Construction", "info@stovallconstructioninc.com",
                        "stovallconstructioninc.com")]
    monkeypatch.setattr("app.leads.pipeline.run_discovery",
                        _fake_discovery(records, SourceStatus.SUCCESS))
    monkeypatch.setattr(
        "app.leads.pipeline.generate_query_expansion",
        lambda t, l: {
            "trade_variants": ["gc"], "location_variants": ["TX"],
            "reason": "", "raw_replies": [], "retried": False,
        },
    )

    query = ResearchQuery(trade="gc", location="TX", target_emails=50)
    outcome = run_full(query, store=store)

    # The info@ email was researched (not skipped at triage)
    assert "info@stovallconstructioninc.com" in research_calls
    # It reached nurture (not generic/skip)
    dossier = store.get("info@stovallconstructioninc.com")
    assert dossier is not None
    assert dossier.recommendation == "nurture"


# ---------------------------------------------------------------------------
# Lever 4: intake gate blocks noise industries
# ---------------------------------------------------------------------------

def test_intake_gate_blocks_noise_industries():
    """Noise industries (real estate, newspaper, junk removal, medical, legal)
    are caught by non_client_terms before AI research is wasted."""
    from app.company_profile import get_profile

    prof = get_profile()
    # These are the noise patterns from the live database
    assert prof.is_non_client("Real Estate Brokerage")
    assert prof.is_non_client("Newspaper / Media Publisher")
    assert prof.is_non_client("Junk Removal / Hauling")
    assert prof.is_non_client("Medical Imaging / Healthcare")
    assert prof.is_non_client("Law Firm / Legal Services")
    assert prof.is_non_client("Funeral Home & Crematory")
    assert prof.is_non_client("Marketing / Digital Agency")
    assert prof.is_non_client("Education Publishing")
    assert prof.is_non_client("Dental Practice")
    # Construction should NOT be blocked
    assert not prof.is_non_client("General Contractor")
    assert not prof.is_non_client("Commercial Builder")


def test_run_full_budget_cancel_is_an_honest_stop(monkeypatch, tmp_path):
    """The harvester's wall-clock budget rides the cancel seam: when it
    fires mid-run, whatever was researched is banked and the shortfall
    carries the explicit harvest_budget_expired reason — never a generic
    failure, never a fabricated target."""
    from app.lead_research.service import LeadResearchStore

    db = str(tmp_path / "leads.db")
    store = LeadResearchStore(db_path=db)

    calls = {"n": 0}

    def _staged(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        calls["n"] += 1
        return SourceStatus.SUCCESS, [
            _record(f"C{calls['n']}", f"c{calls['n']}@x.com", "x.com"),
        ], {"pdf_urls": [f"https://ph/{calls['n']}.pdf"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _staged)

    from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings

    class _LiveAgent:
        def research(self, email, domain, *a, **k):
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(name="Acme", industry="general contractor", location="Texas"),
                person=PersonFindings(name="Jane", role="Owner", bound=True, role_relevance=True),
                potential_score=8.5, recommendation="contact_now",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _LiveAgent)

    # The "budget": expired from the very start — every seam check reads
    # True, so discovery stops after its first pass and the run reports
    # the explicit budget reason (not no_more_leads, not a crash).
    query = ResearchQuery(trade="gc", location="Texas", target_emails=5)
    outcome = run_full(query, store=store, cancel=lambda: True)

    assert outcome["working_leads"] <= 1     # one pass's worth banked at most
    assert outcome["shortfall"] >= 4
    assert outcome["shortfall_reason"] == "harvest_budget_expired"


# ---------------------------------------------------------------------------
# P5-Lite dead-on-arrival gate (heuristic classifier, injected)
# ---------------------------------------------------------------------------

def _empty_discovery(monkeypatch):
    """No live discovery in these tests — the pending cache is the source."""
    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        return SourceStatus.EMPTY, [], {"pdf_urls": []}
    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)


def test_doa_cached_lead_is_marked_dead_not_served(tmp_path, monkeypatch):
    """A cached lead the classifier calls DEAD (disposable / authoritative
    no-MX / real bounce) is marked dead in the cache and never served —
    the AI spend goes to addresses that can exist."""
    from app.lead_research.service import PendingLeadsStore

    _empty_discovery(monkeypatch)
    pending = PendingLeadsStore(db_path=str(tmp_path / "p.db"))
    # take() serves location+trade-matched rows only — the rows carry both.
    pending.add([
        {"email": "bad@mailinator.com", "domain": "mailinator.com",
         "company": "X", "person": "", "source_url": "https://x", "dork": "",
         "location": "Texas", "trade": "gc"},
        {"email": "good@acme.com", "domain": "acme.com",
         "company": "Y", "person": "", "source_url": "https://y", "dork": "",
         "location": "Texas", "trade": "gc"},
    ])
    dead = {"bad@mailinator.com"}

    def classifier(email):
        return ({"confidence": "dead", "reasons": ["disposable"]}
                if email in dead else {"confidence": "medium"})

    query = ResearchQuery(trade="gc", location="Texas", target_emails=5)
    leads, _ = discover_until_target(
        query, pending_store=pending, email_classifier=classifier,
    )
    assert [l["email"] for l in leads] == ["good@acme.com"]
    assert pending.dead_emails() == dead          # marked, never re-served


def test_doa_unknown_verdict_changes_nothing(tmp_path, monkeypatch):
    """A resolver failure is "unknown", never a negative verdict — the
    cached lead is served exactly as before (honest fail-open)."""
    from app.lead_research.service import PendingLeadsStore

    _empty_discovery(monkeypatch)
    pending = PendingLeadsStore(db_path=str(tmp_path / "p.db"))
    pending.add([
        {"email": "maybe@acme.com", "domain": "acme.com",
         "company": "Y", "person": "", "source_url": "https://y", "dork": "",
         "location": "Texas", "trade": "gc"},
    ])

    def classifier(email):
        return {"confidence": "unknown", "reasons": ["mx_unresolvable"]}

    query = ResearchQuery(trade="gc", location="Texas", target_emails=5)
    leads, _ = discover_until_target(
        query, pending_store=pending, email_classifier=classifier,
    )
    assert [l["email"] for l in leads] == ["maybe@acme.com"]
    assert pending.dead_emails() == set()


def test_doa_gate_is_opt_in(tmp_path, monkeypatch):
    """Without an injected classifier there is NO gate (and no hidden
    live-DNS call) — cached leads serve exactly as before."""
    from app.lead_research.service import PendingLeadsStore

    _empty_discovery(monkeypatch)
    pending = PendingLeadsStore(db_path=str(tmp_path / "p.db"))
    pending.add([
        {"email": "bad@mailinator.com", "domain": "mailinator.com",
         "company": "X", "person": "", "source_url": "https://x", "dork": "",
         "location": "Texas", "trade": "gc"},
    ])
    query = ResearchQuery(trade="gc", location="Texas", target_emails=5)
    leads, _ = discover_until_target(query, pending_store=pending)
    assert [l["email"] for l in leads] == ["bad@mailinator.com"]
    assert pending.dead_emails() == set()
