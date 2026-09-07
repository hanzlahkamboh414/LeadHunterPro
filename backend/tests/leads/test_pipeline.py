"""Leads pipeline orchestration — offline tests (fake discovery + fake agent)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.discovery.sources.status import SourceStatus
from app.lead_research.agent import DEAD_DOMAIN_MARKER
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
        "plan_holder": {
            "domain": domain,
            "emails": [{"email": email}],
            "person": {"name": person},
        },
    }


def _fake_discovery(records, status=SourceStatus.SUCCESS):
    def _discover(trade, location, limit, skip_pdfs=None):
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
        lambda skip=None: fake_orch,
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
        lambda skip=None: fake_orch,
    )
    status, records, meta = run_discovery("gc", "TX", 10, skip_pdfs=set())
    assert status == SourceStatus.EMPTY
    assert records == []
    assert meta["reason"]  # auditable stop reason, never silent


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

    def _sequential(trade, location, limit, skip_pdfs=None):
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

    def _discover(trade, location, limit, skip_pdfs=None):
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

    def _advancing(trade, location, limit, skip_pdfs=None):
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

    def _empty(trade, location, limit, skip_pdfs=None):
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
    def _with_stats(trade, location, limit, skip_pdfs=None):
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
    # Pre-stock the cache with surplus leads from a previous run.
    pending.add([
        {"email": "a@x.com", "domain": "x.com", "company": "A", "location": "Texas"},
        {"email": "b@y.com", "domain": "y.com", "company": "B", "location": "Texas"},
        {"email": "c@z.com", "domain": "z.com", "company": "C", "location": "Texas"},
    ])

    # If discovery is called, that means the cache was NOT used — fail.
    def _should_not_run(trade, location, limit, skip_pdfs=None):
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
    def _should_not_run(trade, location, limit, skip_pdfs=None):
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
    pending.add([
        {"email": "a@x.com", "domain": "x.com", "company": "A", "location": "Texas"},
        {"email": "b@y.com", "domain": "y.com", "company": "B", "location": "Texas"},
        {"email": "c@z.com", "domain": "z.com", "company": "C", "location": "Texas"},
    ])
    assert pending.count() == 3

    def _should_not_run(trade, location, limit, skip_pdfs=None):
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
    def _one_per_pass(trade, location, limit, skip_pdfs=None):
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
        def save(self, dossier):
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
            # The cached branch reads real dossier fields (person.name, etc.).
            if email not in self._preexisting:
                return None
            return SimpleNamespace(
                company=SimpleNamespace(name="Pre"),
                person=SimpleNamespace(name="Old", bound=False),
                potential_score=5.0,
                recommendation="nurture",
                intent=None,
                timing=None,
                sources_checked=[],
            )
        def save(self, dossier):
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
        def save(self, dossier):
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
        def save(self, dossier):
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


def test_run_full_keeps_gathering_until_working_target(monkeypatch, tmp_path):
    """'jitni quantity likho utna working data': dead/refined-out emails never
    count toward the target — run_full tops up with more discovery until N
    VISIBLE dossiers persist. Never fabricates: if the sources drain below the
    target, it reports the shortfall honestly instead."""
    from app.lead_research.service import LeadResearchStore

    db = str(tmp_path / "leads.db")
    store = LeadResearchStore(db_path=db)

    calls = {"n": 0}

    def _staged(trade, location, limit, skip_pdfs=None):
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
        def save(self, dossier):
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
