"""Data model roundtrip tests for AIEvidence, CompanyProfile, etc."""

from app.lead_research.models import (
    AIEvidence,
    CompanyProfile,
    IntentAssessment,
    LeadDossier,
    PersonFindings,
    TimingAssessment,
)


def test_aievidence_roundtrip():
    e = AIEvidence(
        claim="Company is in Dallas, TX",
        source_url="https://example.com/about",
        source_type="website",
        confidence="verified",
    )
    d = e.to_dict()
    e2 = AIEvidence.from_dict(d)
    assert e2.claim == "Company is in Dallas, TX"
    assert e2.source_url == "https://example.com/about"
    assert e2.confidence == "verified"


def test_aievidence_unverified():
    e = AIEvidence(claim="No source", confidence="unverified")
    d = e.to_dict()
    assert d["confidence"] == "unverified"
    assert d["source_url"] == ""


def test_company_profile_roundtrip():
    cp = CompanyProfile(
        name="Acme Construction",
        industry="General Contractor",
        location="Dallas, TX",
        website="https://acme.com",
        facts=[
            AIEvidence(claim="Founded in 1990", source_url="https://acme.com/about"),
            AIEvidence(claim="Revenue $10M", source_url="", confidence="unverified"),
        ],
    )
    d = cp.to_dict()
    cp2 = CompanyProfile.from_dict(d)
    assert cp2.name == "Acme Construction"
    assert len(cp2.facts) == 2
    assert cp2.facts[0].confidence == "verified"
    assert cp2.facts[1].confidence == "unverified"


def test_company_profile_client_verdict_roundtrip():
    """The Stage-1 client-fit verdict survives to_dict/from_dict, and an old
    dossier without the fields deserializes to empty (never crashes)."""
    cp = CompanyProfile(
        name="Beta Engineering",
        industry="Engineering Consultancy",
        is_our_client="no",
        client_reason="A/E/C consultant, not a bidding contractor.",
    )
    d = cp.to_dict()
    assert d["is_our_client"] == "no"
    assert d["client_reason"] == "A/E/C consultant, not a bidding contractor."
    cp2 = CompanyProfile.from_dict(d)
    assert cp2.is_our_client == "no"
    assert cp2.client_reason == "A/E/C consultant, not a bidding contractor."

    # Old dossier (fields absent) → empty, no KeyError.
    legacy = CompanyProfile.from_dict({"name": "Old Co", "industry": "GC"})
    assert legacy.is_our_client == ""
    assert legacy.client_reason == ""


def test_person_findings_roundtrip():
    pf = PersonFindings(
        name="John Smith",
        role="Estimator",
        role_relevance=True,
        bound=True,
        evidence=[AIEvidence(claim="Listed on about page", source_url="https://example.com/about")],
    )
    d = pf.to_dict()
    pf2 = PersonFindings.from_dict(d)
    assert pf2.name == "John Smith"
    assert pf2.role_relevance is True
    assert pf2.bound is True
    assert len(pf2.evidence) == 1


def test_intent_assessment_roundtrip():
    ia = IntentAssessment(
        needs_estimation="yes",
        signal="Active bid activity",
        reason="Company regularly bids on projects",
        evidence=[AIEvidence(claim="Bid history", source_url="https://example.com/bids")],
    )
    d = ia.to_dict()
    ia2 = IntentAssessment.from_dict(d)
    assert ia2.needs_estimation == "yes"
    assert ia2.signal == "Active bid activity"
    assert len(ia2.evidence) == 1


def test_timing_assessment_roundtrip():
    ta = TimingAssessment(
        window="soon",
        reason="Q1 bid season starting",
        events=[AIEvidence(claim="RFP posted Dec 2025", source_url="https://example.com/rfp")],
    )
    d = ta.to_dict()
    ta2 = TimingAssessment.from_dict(d)
    assert ta2.window == "soon"
    assert len(ta2.events) == 1


def test_lead_dossier_roundtrip():
    ld = LeadDossier(
        email="john@acme.com",
        domain="acme.com",
        refined_domain="acme.com",
        refined_company="Acme Construction",
        company=CompanyProfile(name="Acme Construction", industry="General Contractor"),
        person=PersonFindings(name="John Smith", role="Estimator", bound=True),
        intent=IntentAssessment(needs_estimation="yes", signal="Active bids"),
        timing=TimingAssessment(window="now", reason="Bid deadline in 2 weeks"),
        fit="Strong fit — active GC needing estimation",
        potential_score=7.5,
        recommendation="contact_now",
        sources_checked=["website", "search"],
        source_errors={"tavily": "timeout"},
        signal_intelligence={"recommended_angle": "Bid Volume Support"},
    )
    d = ld.to_dict()
    ld2 = LeadDossier.from_dict(d)
    assert ld2.email == "john@acme.com"
    assert ld2.refined_domain == "acme.com"
    assert ld2.company.name == "Acme Construction"
    assert ld2.person.name == "John Smith"
    assert ld2.intent.needs_estimation == "yes"
    assert ld2.timing.window == "now"
    assert ld2.potential_score == 7.5
    assert ld2.recommendation == "contact_now"
    assert ld2.sources_checked == ["website", "search"]
    assert ld2.source_errors == {"tavily": "timeout"}
    assert ld2.signal_intelligence == {"recommended_angle": "Bid Volume Support"}


def test_lead_dossier_defaults():
    ld = LeadDossier(email="test@test.com", domain="test.com")
    assert ld.refined_domain == ""
    assert ld.company.name == ""
    assert ld.person.name == ""
    assert ld.intent.needs_estimation == "unknown"
    assert ld.timing.window == "unknown"
    assert ld.potential_score == 0.0
    assert ld.recommendation == "skip"
    assert ld.sources_checked == []
    assert ld.source_errors == {}
    assert ld.signal_intelligence == {}
