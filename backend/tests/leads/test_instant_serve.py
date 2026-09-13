"""P7 — instant serve (emails vertical): SHARED already-researched dossiers
serve at search time — pure SQL + a race-guarded ownership mark, zero AI
spend. These pin the store contract (:meth:`serve_shared`) and run_full's
Phase 0 wiring (instant fulfil -> no discovery; partial -> discovery chases
only the remainder, merged against the ORIGINAL target). The phones side of
P7 (pure-SQL search) has its own contracts in tests/phones/.
"""

from __future__ import annotations

from app.discovery.sources.status import SourceStatus
from app.lead_research.models import (
    CompanyProfile,
    LeadDossier,
    PersonFindings,
)
from app.lead_research.service import LeadResearchStore
from app.leads.pipeline import ResearchQuery, run_full


def _dossier(email: str, *, industry: str = "general contractor",
             location: str = "Dallas, Texas", score: float = 8.5) -> LeadDossier:
    """A VISIBLE dossier (passes regate: construction industry, relevant
    role, bound, score well above the nurture threshold)."""
    return LeadDossier(
        email=email, domain=email.split("@", 1)[1],
        company=CompanyProfile(name="Acme", industry=industry, location=location),
        person=PersonFindings(name="Jane", role="Owner", bound=True,
                              role_relevance=True),
        potential_score=score, recommendation="contact_now",
    )


def _stock_shared(store: LeadResearchStore, emails: list[str], **kw) -> None:
    """Stock the shared pool the way the harvester does: user_id='' saves."""
    for e in emails:
        store.save(_dossier(e, **kw), user_id="")


def _record(name: str, email: str, domain: str) -> dict:
    return {
        "company_name": name,
        "source_url": f"https://{domain}",
        "trade_category": "general_contractor",
        "plan_holder": {"domain": domain,
                        "emails": [{"email": email}],
                        "person": {"name": ""}},
    }


# ---------------------------------------------------------------------------
# serve_shared — the store contract
# ---------------------------------------------------------------------------

def test_serve_shared_claims_ownerless_shared_dossiers(tmp_path):
    store = LeadResearchStore(str(tmp_path / "leads.db"))
    _stock_shared(store, ["a@x.com", "b@y.com"])

    served = store.serve_shared(2, trade="gc", location="Texas", user_id="bob")
    assert [d.email for d in served] == ["a@x.com", "b@y.com"]
    # Claimed: bob's dashboard shows them, and nobody else can claim them.
    assert len(store.list_all(user_id="bob")) == 2
    assert store.serve_shared(5, trade="gc", location="Texas",
                              user_id="carol") == []


def test_serve_shared_excludes_owned_leads(tmp_path):
    """Exclusivity at serve: another user's lead and the caller's own earlier
    claim are both NOT servable — only ownerless shared rows qualify."""
    store = LeadResearchStore(str(tmp_path / "leads.db"))
    store.save(_dossier("theirs@x.com"), user_id="alice")
    _stock_shared(store, ["shared@y.com"])
    # bob already claimed this one in an earlier run.
    store.serve_shared(1, trade="gc", location="Texas", user_id="bob")
    _stock_shared(store, ["shared@y.com"])  # re-save keeps it bob's (upsert)

    served = store.serve_shared(5, trade="gc", location="Texas", user_id="bob")
    # theirs@ = alice's; shared@ = bob's own earlier claim — neither serves.
    assert served == []


def test_serve_shared_trade_gate(tmp_path):
    store = LeadResearchStore(str(tmp_path / "leads.db"))
    _stock_shared(store, ["r@x.com"], industry="roofing")

    # A gc search never serves roofing inventory (the P2 trade gate)...
    assert store.serve_shared(5, trade="gc", location="Texas", user_id="bob") == []
    # ...a roofing search does, and no gate at all is fail-open.
    assert [d.email for d in store.serve_shared(
        5, trade="roofing", location="Texas", user_id="bob")] == ["r@x.com"]
    _stock_shared(store, ["g@y.com"])
    assert len(store.serve_shared(5, location="Texas", user_id="carol")) == 1


def test_serve_shared_state_level_location_match(tmp_path):
    """Location matching is STATE-level: Dallas search serves a Houston
    dossier (both TX), never a Miami one (FL); an unknown dossier location
    is fail-open (the P2 rule — never starve on a labeling gap)."""
    store = LeadResearchStore(str(tmp_path / "leads.db"))
    _stock_shared(store, ["h@x.com"], location="Houston, Texas")
    _stock_shared(store, ["m@x.com"], location="Miami, Florida")
    _stock_shared(store, ["u@x.com"], location="")

    served = store.serve_shared(5, trade="gc", location="Dallas, TX",
                                user_id="bob")
    assert {d.email for d in served} == {"h@x.com", "u@x.com"}


def test_serve_shared_visible_only(tmp_path):
    """The read-time regate is the gate: skip-scored and non-construction
    dossiers never serve, whatever their stored recommendation says."""
    store = LeadResearchStore(str(tmp_path / "leads.db"))
    _stock_shared(store, ["low@x.com"], score=1.0)
    _stock_shared(store, ["cafe@x.com"], industry="Coffee Shop")
    _stock_shared(store, ["ok@x.com"])

    served = store.serve_shared(5, trade="gc", location="Texas", user_id="bob")
    assert [d.email for d in served] == ["ok@x.com"]


def test_serve_shared_hidden_never_serves(tmp_path):
    store = LeadResearchStore(str(tmp_path / "leads.db"))
    _stock_shared(store, ["a@x.com"])
    store.set_hidden("a@x.com", True)
    assert store.serve_shared(5, trade="gc", location="Texas",
                              user_id="bob") == []


# ---------------------------------------------------------------------------
# run_full Phase 0 — instant fulfil + partial remainder
# ---------------------------------------------------------------------------

def test_run_full_instant_fulfils_target_without_discovery(monkeypatch, tmp_path):
    """The harvester-stocked pool alone meets the target -> the run IS
    instant: no discovery pass, no research spend, instant entries counted
    as working output."""
    store = LeadResearchStore(str(tmp_path / "leads.db"))
    _stock_shared(store, ["a@x.com", "b@y.com"])

    def _should_not_run(*a, **k):
        raise AssertionError("pool fulfilled the target — no discovery needed")

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _should_not_run)

    events: list[dict] = []

    def emit(phase, step, total, message, email="", data=None):
        events.append({"phase": phase, "email": email, "data": data or {}})

    query = ResearchQuery(trade="gc", location="Texas", target_emails=2)
    outcome = run_full(query, emit=emit, store=store, user_id="bob")

    assert outcome["working_leads"] == 2
    assert outcome["shortfall"] == 0
    assert outcome["instant_served"] == 2
    assert outcome["discovery_passes"] == []
    assert {e["email"] for e in outcome["results"]} == {"a@x.com", "b@y.com"}
    entry = outcome["results"][0]
    assert entry["instant"] is True and entry["working"] is True
    # The live feed shows them like any research lead (phase="research").
    assert len([e for e in events if e["phase"] == "research"]) == 2


def test_run_full_instant_partial_chases_only_remainder(monkeypatch, tmp_path):
    """Pool has 1 of 2: the instant serve claims it, then discovery chases
    ONLY the remaining 1 — the merged outcome still measures 2/2 against the
    ORIGINAL target."""
    store = LeadResearchStore(str(tmp_path / "leads.db"))
    _stock_shared(store, ["shared@x.com"])

    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None,
                  candidate_store=None):
        return (SourceStatus.SUCCESS,
                [_record("New", "fresh@z.com", "z.com")],
                {"pdf_urls": []})

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)

    class _LiveAgent:
        def research(self, email, domain, *a, **k):
            return _dossier(email, location="Texas")

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _LiveAgent)

    query = ResearchQuery(trade="gc", location="Texas", target_emails=2)
    outcome = run_full(query, store=store, user_id="bob")

    assert outcome["instant_served"] == 1
    assert outcome["working_leads"] == 2
    assert outcome["shortfall"] == 0
    assert outcome["target_emails"] == 2  # the ORIGINAL target, not the remainder
    working = {e["email"] for e in outcome["results"] if e.get("working")}
    assert working == {"shared@x.com", "fresh@z.com"}


def test_run_full_harvester_run_never_instant_serves(monkeypatch, tmp_path):
    """The harvester (user_id='') STOCKS the shared pool — it must never
    drain it back through instant serve."""
    store = LeadResearchStore(str(tmp_path / "leads.db"))
    _stock_shared(store, ["a@x.com"])

    def _empty(trade, location, limit, skip_pdfs=None, yield_store=None,
               candidate_store=None):
        return SourceStatus.SUCCESS, [], {"pdf_urls": []}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _empty)
    query = ResearchQuery(trade="gc", location="Texas", target_emails=5)
    outcome = run_full(query, store=store, user_id="")
    assert outcome["instant_served"] == 0
    # The shared row is untouched — still ownerless for a real user to claim.
    assert store.serve_shared(1, trade="gc", location="Texas",
                              user_id="bob")


def test_instant_leads_are_auto_filed(monkeypatch, tmp_path):
    """Instant-served leads follow the same auto-file policy as researched
    ones — the run's folder + search tag land at serve time."""
    store = LeadResearchStore(str(tmp_path / "leads.db"))
    _stock_shared(store, ["a@x.com"])

    query = ResearchQuery(trade="gc", location="Texas", target_emails=1,
                          search_name="Houston GC Q3", folder="Q3")
    outcome = run_full(query, store=store, user_id="bob")
    assert outcome["instant_served"] == 1
    meta = store.get_meta("a@x.com")
    assert meta is not None
    assert meta.folder == "Q3"
    assert "Houston GC Q3" in meta.tags


def test_second_search_never_double_serves(monkeypatch, tmp_path):
    """The user's own earlier claim is 'already have it', not new working
    data: a repeat search gets an honest shortfall, never an inflated
    instant serve."""
    store = LeadResearchStore(str(tmp_path / "leads.db"))
    _stock_shared(store, ["a@x.com"])

    def _empty(trade, location, limit, skip_pdfs=None, yield_store=None,
               candidate_store=None):
        return SourceStatus.SUCCESS, [], {"pdf_urls": []}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _empty)
    query = ResearchQuery(trade="gc", location="Texas", target_emails=1)
    first = run_full(query, store=store, user_id="bob")
    assert first["instant_served"] == 1

    second = run_full(query, store=store, user_id="bob")
    assert second["instant_served"] == 0
    assert second["working_leads"] == 0
    assert second["shortfall"] == 1
