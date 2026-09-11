"""Phase G — Layer-1 dork-yield credit flows through the research pipeline.

The discovery layer records a dork template's DISPATCH; the research layer
credits ``working`` when a dork-attributed company becomes a WORKING lead (a
visible dossier). This file pins that attribution bridge end to end.
"""

from __future__ import annotations

from app.discovery.yield_learning import DiscoveryYieldStore, segment_key
from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.lead_research.service import LeadResearchStore
from app.leads.pipeline import run_research


def _dossier(email: str, domain: str, recommendation: str, score: float) -> LeadDossier:
    return LeadDossier(
        email=email,
        domain=domain,
        company=CompanyProfile(
            name="Acme Construction", industry="general contractor", location="TX",
        ),
        person=PersonFindings(name="Jane", role="Owner", bound=True,
                              role_relevance=True),
        recommendation=recommendation,
        potential_score=score,
    )


class _WorkingAgent:
    def __init__(self, *a, **k):
        pass

    def research(self, email, domain, *a, **k):
        return _dossier(email, domain, "contact_now", 8.5)


class _SkipAgent:
    def __init__(self, *a, **k):
        pass

    def research(self, email, domain, *a, **k):
        return _dossier(email, domain, "skip", 2.0)


def _lead(email="a@acme.com", domain="acme.com", dork="plan_holder:A") -> dict:
    return {
        "email": email, "domain": domain, "company": "Acme Construction",
        "source_url": "https://x.example/plan.pdf", "_discovery_dork": dork,
    }


def test_working_dork_is_credited(monkeypatch, tmp_path):
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _WorkingAgent)
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    results = run_research([_lead()], store=store)
    assert results[0]["working"] is True
    assert DiscoveryYieldStore(db).get("plan_holder:A") == {"trials": 0, "working": 1}


def test_non_working_dork_is_not_credited(monkeypatch, tmp_path):
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _SkipAgent)
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    results = run_research([_lead()], store=store)
    assert results[0]["working"] is False
    assert DiscoveryYieldStore(db).all() == {}


def test_no_credit_without_dork_attribution(monkeypatch, tmp_path):
    """A company with no dork attribution (e.g. served from a cache a later
    run, or from a seam with no per-dork signal) credits NOTHING — silence is
    not evidence."""
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _WorkingAgent)
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    lead = _lead()
    lead["_discovery_dork"] = ""  # unattributable
    run_research([lead], store=store)
    assert DiscoveryYieldStore(db).all() == {}


def test_multiple_working_leads_accumulate(monkeypatch, tmp_path):
    """Two working companies from the same dork credit it twice — the signal
    a dork produces real leads is unmistakable, never averaged away."""
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _WorkingAgent)
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    leads = [
        _lead("a@acme.com", "acme.com", "plan_holder:A"),
        _lead("b@bldg.com", "bldg.com", "plan_holder:A"),
    ]
    run_research(leads, store=store)
    assert DiscoveryYieldStore(db).get("plan_holder:A")["working"] == 2


def test_working_credit_carries_segment(monkeypatch, tmp_path):
    """Phase I — a working lead credits the (dork, trade|location) SEGMENT row,
    not the global one: the run's trade/location identify which discovery
    segment proved the dork."""
    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _WorkingAgent)
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    run_research([_lead()], store=store, trade="roofing", location="Dallas TX")
    seg = segment_key("roofing", "Dallas TX")
    assert DiscoveryYieldStore(db).get("plan_holder:A", seg) == {
        "trials": 0, "working": 1,
    }
