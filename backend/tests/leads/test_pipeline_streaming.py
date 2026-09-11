"""Phase D — streaming producer/consumer pipeline tests.

run_full dispatches to the streaming producer/consumer path when a real
persistence store + discovery cache are present AND research concurrency > 1.
These tests exercise that path with REAL LeadResearchStore + PendingLeadsStore
on tmp files (same SQLite concurrency the job runner hits) and fake discovery
/ agent lanes.

The user's Phase D contract:
  * producer keeps finding data and STORING it in one place (the pending
    buffer) while consumer workers research WITHOUT stopping;
  * production stops once the working target is met;
  * leftover buffered data is researched + shown on the frontend (drained),
    never skipped;
  * research never duplicates a buffered row (claim guard);
  * a skip/dead/error pool stops production honestly (no infinite grind).
"""

from __future__ import annotations

import time

from app.discovery.sources.status import SourceStatus
from app.lead_research.agent import DEAD_DOMAIN_MARKER
from app.leads.pipeline import ResearchQuery, run_full


def _record(company: str, email: str, domain: str) -> dict:
    return {
        "company_name": company,
        "source_url": "https://x.example",
        "plan_holder": {
            "domain": domain,
            "emails": [{"email": email}],
            "person": {"name": "Jane"},
        },
    }


def _stores(tmp_path):
    """A real persistence store + discovery cache over one SQLite file."""
    from app.lead_research.service import LeadResearchStore, PendingLeadsStore

    db = str(tmp_path / "leads.db")
    store = LeadResearchStore(db_path=db)
    pending = PendingLeadsStore(db_path=db)
    return db, store, pending


def _discovery_returns(*batches):
    """Fake run_discovery returning each batch once, then empty (drained)."""
    calls = {"n": 0}

    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        i = calls["n"]
        calls["n"] += 1
        if i >= len(batches):
            return SourceStatus.EMPTY, [], {"pdf_urls": [f"https://ph/{i}.pdf"]}
        records = batches[i]
        return SourceStatus.SUCCESS, records, {"pdf_urls": [f"https://ph/{i}.pdf"]}

    return _discover, calls


def _ok_agent(monkeypatch, *, delay: float = 0.0):
    """Fake AILeadResearchAgent returning a visible (contact_now) dossier."""
    from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings

    calls: list[tuple] = []

    class _Agent:
        def research(self, email, domain, *a, **k):
            if delay:
                time.sleep(delay)
            calls.append((email, domain))
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(name="Acme", industry="general contractor",
                                       location="Texas"),
                person=PersonFindings(name="Jane", role="Owner", bound=True,
                                      role_relevance=True),
                potential_score=8.5, recommendation="contact_now",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _Agent)
    return calls


# ---------------------------------------------------------------------------
# Core Phase D contract
# ---------------------------------------------------------------------------

def test_streaming_reaches_target_and_drains_leftover(monkeypatch, tmp_path):
    """The producer over-delivers one big batch; EVERY discovered lead is
    researched and shown (leftover never skipped), and the working target is
    reached. Buffer is drained to empty by the end."""
    db, store, pending = _stores(tmp_path)
    calls = _ok_agent(monkeypatch)
    records = [_record(f"C{i}", f"a{i}@x.com", "x.com") for i in range(12)]
    _discover, d_calls = _discovery_returns(records)
    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)

    query = ResearchQuery(trade="gc", location="Texas", target_emails=3)
    outcome = run_full(query, store=store, pending_store=pending)

    # Leftover beyond the target is DRAINED, not dropped: all 12 researched.
    assert len(outcome["results"]) == 12
    working = [e for e in outcome["results"] if e.get("working")]
    assert len(working) == 12
    # Any working lead counted toward the target.
    assert outcome["working_leads"] == 12
    assert outcome["shortfall"] == 0
    # Every lead researched exactly once AND persisted to the dossiers store.
    assert {e["email"] for e in outcome["results"]} == {f"a{i}@x.com" for i in range(12)}
    for i in range(12):
        assert store.get(f"a{i}@x.com") is not None
    # Buffer fully drained — nothing left to skip in a later run.
    remaining = pending.take(100, location="Texas")
    assert remaining == []
    # Demand was met quickly: a few discovery passes, not a grind.
    assert d_calls["n"] <= 20
    assert calls  # research actually ran through the consumer threads


def test_streaming_researches_each_lead_once(monkeypatch, tmp_path):
    """Concurrent consumers must NEVER research the same buffered lead twice —
    the in-memory claim guard (peek-not-pop take) makes each claim exclusive."""
    db, store, pending = _stores(tmp_path)
    calls = _ok_agent(monkeypatch, delay=0.03)  # overlap window for racing
    records = [_record(f"C{i}", f"a{i}@x.com", "x.com") for i in range(20)]
    _discover, _ = _discovery_returns(records)
    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)

    query = ResearchQuery(trade="gc", location="Texas", target_emails=2)
    outcome = run_full(query, store=store, pending_store=pending)

    emails = [e["email"] for e in outcome["results"]]
    assert len(emails) == len(set(emails)) == 20  # no duplicate row researched
    assert len(calls) == 20
    assert {c[0] for c in calls} == {f"a{i}@x.com" for i in range(20)}


# ---------------------------------------------------------------------------
# Honest bad-lead handling
# ---------------------------------------------------------------------------

def test_streaming_dead_domain_flagged_never_re_served(monkeypatch, tmp_path):
    """A dead-domain lead is flagged (never persisted, never counted) and its
    buffered row is excluded, so it can never re-appear as a recurring Skip."""
    from app.lead_research.models import (CompanyProfile, LeadDossier,
                                          PersonFindings)

    db, store, pending = _stores(tmp_path)

    class _DeadAgent:
        def research(self, email, domain, *a, **k):
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(name="", industry="general contractor",
                                       location="TX"),
                person=PersonFindings(name="", role="", bound=False,
                                      role_relevance=False),
                potential_score=0.0, recommendation="skip",
                fit=f"Dead/expired domain — {DEAD_DOMAIN_MARKER} (undeliverable)",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _DeadAgent)
    records = [_record("Dead", f"d{i}@gone.com", "gone.com") for i in range(10)]
    _discover, _ = _discovery_returns(records)
    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)

    query = ResearchQuery(trade="gc", location="Texas", target_emails=5)
    outcome = run_full(query, store=store, pending_store=pending)

    assert outcome["working_leads"] == 0  # none visible, never counted
    for i in range(10):
        assert store.get(f"d{i}@gone.com") is None  # never persisted
    # The rows are flagged dead (kept for the audit trail, invisible to take).
    assert pending.dead_emails() == {f"d{i}@gone.com" for i in range(10)}
    assert pending.take(100, location="Texas") == []  # never served again


def test_streaming_error_marks_attempt_and_cooldown(monkeypatch, tmp_path):
    """A research ERROR leaves the buffered row in pending (a real lead, not
    dead) with an attempted_at stamp — take() cools it down instead of
    re-serving and re-failing it, and one failure never kills the other rows."""
    db, store, pending = _stores(tmp_path)
    calls = {"n": 0}

    class _Agent:
        def research(self, email, domain, *a, **k):
            calls["n"] += 1
            if email in {"a0@x.com", "a1@x.com"}:
                raise RuntimeError("LLM unavailable")
            from app.lead_research.models import (CompanyProfile, LeadDossier,
                                                  PersonFindings)
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(name="Acme", industry="general contractor",
                                       location="Texas"),
                person=PersonFindings(name="Jane", role="Owner", bound=True,
                                      role_relevance=True),
                potential_score=8.5, recommendation="contact_now",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _Agent)
    records = [_record(f"C{i}", f"a{i}@x.com", "x.com") for i in range(6)]
    _discover, _ = _discovery_returns(records)
    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)

    query = ResearchQuery(trade="gc", location="Texas", target_emails=4)
    outcome = run_full(query, store=store, pending_store=pending)

    # The 4 healthy leads are researched AND visible; the 2 failed are not.
    assert outcome["working_leads"] == 4
    assert {e["email"] for e in outcome["results"] if e.get("working")} == {
        f"a{i}@x.com" for i in range(2, 6)
    }
    errors = [e for e in outcome["results"] if e.get("error")]
    assert {e["email"] for e in errors} == {"a0@x.com", "a1@x.com"}
    # Failed rows stay in pending but are excluded by the cooldown (retry later).
    assert pending.cooling_emails(86400) >= {"a0@x.com", "a1@x.com"}
    # With cooldown active, a normal take won't re-serve the failed rows.
    served = pending.take(100, location="Texas", cooldown_seconds=86400)
    assert {l["email"] for l in served} < {"a0@x.com", "a1@x.com"}


def test_streaming_plateau_stops_honestly(monkeypatch, tmp_path):
    """Every discovered lead scores skip (non-client slip / score-skip): the
    consumers report consecutive non-working results, the producer STOPS with an
    honest reason instead of streaming the same dead end forever (async Fix C)."""
    from app.lead_research.models import (CompanyProfile, LeadDossier,
                                          PersonFindings)
    from app.leads.pipeline import _STREAM_PLATEAU_NONWORKING

    db, store, pending = _stores(tmp_path)
    calls = {"n": 0}

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

    def _endless(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        calls["n"] += 1
        # Genuinely NEW domain every pass — Fix A can't filter it, so the only
        # thing that stops the run is the research-side plateau guard.
        return SourceStatus.SUCCESS, [
            _record("NoClient", f"skip{calls['n']}@nc{calls['n']}.com",
                    f"nc{calls['n']}.com"),
        ], {"pdf_urls": [f"https://ph/{calls['n']}.pdf"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _endless)

    query = ResearchQuery(trade="gc", location="Texas", target_emails=100)
    outcome = run_full(query, store=store, pending_store=pending)

    assert outcome["working_leads"] == 0
    assert outcome["shortfall"] == 100
    assert outcome["shortfall_reason"] == "no_progress_plateau"
    # Bounded: the plateau is read at END of a full trade×location sweep — a
    # dead per-pass check would pre-empt the other markets this round is still
    # searching (the whole point of the end-of-round reconciliation).  So the
    # run always completes ONE full sweep of the real expansion surface
    # (~56-72 discovery passes) before the guard can fire.
    assert calls["n"] >= _STREAM_PLATEAU_NONWORKING  # ≥ threshold results had to be read
    # And it must NOT grind the entire 100-lead allowance on a pool that yields
    # only skips (a plateau-less producer would chase all 100 targets across
    # several rounds / hundreds of passes).  Stopping inside ~1-2 sweeps proves
    # the guard catches the dead end instead of the run buffering to 100.
    assert calls["n"] <= query.target_emails


def test_streaming_plateau_expands_surface_before_stopping(monkeypatch, tmp_path):
    """Surface Expansion Guard (Inc 1): a research plateau on a known metro is
    not proof the metro is dry — the producer must pull the fallback's un-
    searched markets into the rotation, keep hunting, and stop with
    no_progress_plateau only once the EXPANDED surface is also dry.

    Live proof this guards: the 2026-09-11 Fort Worth run stopped at 5/20 with
    3 markets searched while 6 more sat in the fallback table untouched.
    """
    from app.lead_research.models import (CompanyProfile, LeadDossier,
                                          PersonFindings)

    db, store, pending = _stores(tmp_path)
    calls = {"n": 0, "locations": []}

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

    def _endless(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        calls["n"] += 1
        calls["locations"].append(location)
        return SourceStatus.SUCCESS, [
            _record("NoClient", f"skip{calls['n']}@nc{calls['n']}.com",
                    f"nc{calls['n']}.com"),
        ], {"pdf_urls": [f"https://ph/{calls['n']}.pdf"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _endless)
    # Hermetic: no live AI in tests — expansion returns the literal-only
    # surface (the exact pre-expansion shape), so ALL breadth comes from the
    # deterministic fallback the Surface Expansion Guard pulls in.
    monkeypatch.setattr(
        "app.leads.pipeline.generate_query_expansion",
        lambda t, l: {"trade_variants": [], "location_variants": [],
                      "reason": ""},
    )

    events: list[dict] = []

    def _emit(phase, step, total, message, email="", data=None):
        events.append({"phase": phase, "message": message, "data": data or {}})

    query = ResearchQuery(trade="gc", location="Houston TX", target_emails=100)
    outcome = run_full(query, store=store, pending_store=pending, emit=_emit)

    # Final state is still the honest plateau stop — expansion only DELAYS it
    # until the expanded surface is proven dry too.
    assert outcome["working_leads"] == 0
    assert outcome["shortfall_reason"] == "no_progress_plateau"

    # PROOF 1: the surface expanded mid-run — an expansion event fired naming
    # the newly pulled markets and the producer went on searching them.
    expansions = [e for e in events if e["phase"] == "expansion"
                  and e["data"].get("trigger") == "plateau"]
    assert expansions, "plateau fired but the surface never expanded"
    assert expansions[0]["data"]["new_markets"]  # names the new markets
    assert "Harris County TX" in expansions[0]["data"]["new_markets"]

    # PROOF 2: the producer actually SEARCHED the expanded markets (the
    # rotation reached them), not just announced them.
    searched = set(calls["locations"])
    assert "Harris County TX" in searched
    assert "Conroe TX" in searched

    # PROOF 3: honest final stop — after the expansion pool is exhausted a
    # SECOND plateau breaks the loop (no infinite expansion: the fallback
    # table is finite, so the run terminates).
    assert len(expansions) == 1


# ---------------------------------------------------------------------------
# Producer stops when demand met; serial path stays default for no-cache
# ---------------------------------------------------------------------------

def test_streaming_stops_producing_once_demand_met(monkeypatch, tmp_path):
    """Once the working target is met the producer stops bringing data — it
    does not keep discovering just because a source still has records."""
    db, store, pending = _stores(tmp_path)
    _ok_agent(monkeypatch)
    records = [_record(f"C{i}", f"a{i}@x.com", "x.com") for i in range(8)]
    _discover, d_calls = _discovery_returns(records)
    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)

    query = ResearchQuery(trade="gc", location="Texas", target_emails=2)
    outcome = run_full(query, store=store, pending_store=pending)

    # Demand (2 visible) met out of the single 8-lead batch; every surplus row
    # is drained (shown), so the buffer is empty — no second discovery wave.
    assert outcome["working_leads"] == 8
    # Bounded by base_rounds(3) × passes-per-round(2), NOT a grind over the pool.
    assert d_calls["n"] <= 6


def test_serial_path_used_without_cache(monkeypatch):
    """No store & no pending cache -> run_full stays on the serial round-loop
    (Phase D dispatches only when a REAL persistence store + cache exist so
    consumers can pull from the buffer). A bare run_full is the CLI path."""
    _ok_agent(monkeypatch)
    records = [_record(f"C{i}", f"a{i}@x.com", "x.com") for i in range(3)]
    _discover, _ = _discovery_returns(records)
    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)

    query = ResearchQuery(trade="gc", location="Texas", target_emails=1)
    outcome = run_full(query)  # no store, no pending_store -> serial

    # The serial round-loop researches only what discovery gathered to target
    # (1 lead) — no drain of surplus, no concurrent consumers.
    assert outcome["working_leads"] == 1
    assert outcome["shortfall"] == 0