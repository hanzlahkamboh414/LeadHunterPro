"""LeadScorer — deterministic signal-based scoring tests."""

from __future__ import annotations

from app.lead_research.models import AIEvidence, CompanyProfile, IntentAssessment, LeadDossier, PersonFindings, TimingAssessment
from app.lead_research.scoring import LeadScorer, regate_recommendation


def _company(**kw):
    return CompanyProfile(
        name=kw.get("name", "Acme"),
        industry=kw.get("industry", "GC"),
        location=kw.get("location", "TX"),
        facts=kw.get("facts", []),
    )


def _person(**kw):
    return PersonFindings(
        name=kw.get("name", "John"),
        role=kw.get("role", "Estimator"),
        role_relevance=kw.get("role_relevance", True),
        bound=kw.get("bound", True),
    )


def _intent(**kw):
    return IntentAssessment(
        needs_estimation=kw.get("needs_estimation", "yes"),
        signal=kw.get("signal", "bids"),
    )


def _timing(**kw):
    return TimingAssessment(window=kw.get("window", "now"))


# --- Happy path ---

def test_high_score_contact_now():
    """Construction company + bound person + relevant role + intent = contact_now."""
    scorer = LeadScorer()
    fit, score, rec = scorer.score(
        "x@acme.com",
        _company(name="Acme Construction", industry="general contractor", location="Texas"),
        _person(bound=True, role_relevance=True),
        _intent(needs_estimation="yes", signal="active bids"),
        _timing(window="now"),
    )
    # construction(2.0) + bound(1.5) + role(1.0) + needs_est(2.0) + signal(1.0) + timing(0.5) + area(0.5) = 8.5
    assert score >= 6.0
    assert rec == "contact_now"


def test_medium_score_nurture():
    """Construction company + bound person but no estimation need = nurture."""
    scorer = LeadScorer()
    fit, score, rec = scorer.score(
        "x@acme.com",
        _company(name="Acme Construction", industry="general contractor", location="Texas"),
        _person(bound=True, role_relevance=True),
        _intent(needs_estimation="no", signal=""),
        _timing(window=""),
    )
    # construction(2.0) + bound(1.5) + role(1.0) = 4.5
    assert 3.0 <= score < 6.0
    assert rec == "nurture"


def test_low_score_skip():
    """Non-construction + unbound person + no signals = skip."""
    scorer = LeadScorer()
    fit, score, rec = scorer.score(
        "x@acme.com",
        _company(name="Acme Corp", industry="retail", location="NY"),
        _person(bound=False, role_relevance=False),
        _intent(needs_estimation="no", signal=""),
        _timing(window=""),
    )
    # related(0.5) + non-construction(-1.0) = -0.5
    assert score < 3.0
    assert rec == "skip"


# --- Gate overrides ---

def test_gate_high_score_unbound_downgrades():
    """High score but person not bound → nurture (not contact_now)."""
    scorer = LeadScorer()
    _, score, rec = scorer.score(
        "x@acme.com",
        _company(name="Acme Construction", industry="general contractor", location="Texas"),
        _person(bound=False, role_relevance=True),
        _intent(needs_estimation="yes", signal="expansion"),
        _timing(window="Q1"),
    )
    # construction(2.0) + role(1.0) + needs_est(2.0) + signal(1.0) + timing(0.5) + area(0.5) = 7.0
    # but NOT bound → gate says nurture
    assert score >= 6.0
    assert rec == "nurture"


def test_gate_medium_score_unbound_stays_nurture():
    scorer = LeadScorer()
    _, score, rec = scorer.score(
        "x@acme.com",
        _company(name="Acme", industry="construction", location="TX"),
        _person(bound=False, role_relevance=False),
        _intent(needs_estimation="no", signal="hiring"),
        _timing(window=""),
    )
    # construction(2.0) + non-construction(-1.0 — wait, it IS construction)
    # Actually: construction(2.0) + signal(1.0) = 3.0
    assert score >= 3.0
    assert rec == "nurture"


def test_gate_very_low_skip():
    scorer = LeadScorer()
    _, score, rec = scorer.score(
        "x@acme.com",
        _company(name="Acme", industry="retail", location="NY"),
        _person(bound=False, role_relevance=False),
        _intent(needs_estimation="no", signal=""),
        _timing(window=""),
    )
    assert score < 3.0
    assert rec == "skip"


def test_gate_exact_6_bound_is_contact_now():
    """Score exactly 6.0 with bound person → contact_now."""
    scorer = LeadScorer()
    # construction(2.0) + bound(1.5) + role(1.0) + needs_est(2.0) = 6.5 (>= 6.0)
    _, _, rec = scorer.score(
        "x@acme.com",
        _company(name="Acme", industry="construction", location="NY"),
        _person(bound=True, role_relevance=True),
        _intent(needs_estimation="yes", signal=""),
        _timing(window=""),
    )
    assert rec == "contact_now"


def test_gate_exact_3_unbound_is_nurture():
    """Score exactly 3.0 with unbound person → nurture."""
    scorer = LeadScorer()
    # construction(2.0) + signal(1.0) = 3.0
    _, score, rec = scorer.score(
        "x@acme.com",
        _company(name="Acme", industry="construction", location="NY"),
        _person(bound=False, role_relevance=False),
        _intent(needs_estimation="no", signal="hiring"),
        _timing(window=""),
    )
    assert score >= 3.0
    assert rec == "nurture"


# --- Edge cases ---

def test_no_company_name():
    """No company name → low score."""
    scorer = LeadScorer()
    _, score, rec = scorer.score(
        "x@unknown.com",
        _company(name="", industry="", location=""),
        _person(bound=False, role_relevance=False),
        _intent(needs_estimation="no", signal=""),
        _timing(window=""),
    )
    assert score < 3.0
    assert rec == "skip"


def test_construction_with_evidence_facts():
    """Construction company with evidence facts gets bonus."""
    scorer = LeadScorer()
    facts = [AIEvidence(claim="Active projects", source_url="https://example.com", source_type="web", confidence="high")]
    _, score, rec = scorer.score(
        "x@acme.com",
        _company(name="Acme", industry="construction", location="TX", facts=facts),
        _person(bound=True, role_relevance=True),
        _intent(needs_estimation="yes", signal="bids"),
        _timing(window="now"),
    )
    # construction(2.0) + bound(1.5) + role(1.0) + needs_est(2.0) + signal(1.0) + timing(0.5) + area(0.5) + evidence(0.5) = 9.0
    assert score >= 8.0
    assert rec == "contact_now"


def test_free_mail_nurture():
    """Free mail domain still gets scored (triage handles it separately)."""
    scorer = LeadScorer()
    _, score, rec = scorer.score(
        "john@gmail.com",
        _company(name="", industry="", location=""),
        _person(bound=False, role_relevance=False),
        _intent(needs_estimation="no", signal=""),
        _timing(window=""),
    )
    # No company info → negative score → skip
    assert rec == "skip"


def test_non_construction_high_score_never_contact_now():
    """IT/software company with all the right signals still HARD-SKIPS —
    The Best Estimator sells to construction, an IT company is worthless."""
    scorer = LeadScorer()
    _, score, rec = scorer.score(
        "x@itech.com",
        _company(name="ITech Solutions", industry="IT Services", location="Texas"),
        _person(bound=True, role_relevance=True),
        _intent(needs_estimation="yes", signal="active bids"),
        _timing(window="now"),
    )
    # Old behavior: score >= 6.0 + bound → contact_now (wrong). New: skip.
    assert score >= 6.0  # signals would otherwise qualify
    assert rec == "skip"


def test_non_construction_skip_even_with_high_signals():
    """Non-construction is a hard reject regardless of intent/timing strength."""
    scorer = LeadScorer()
    _, score, rec = scorer.score(
        "x@soft.com",
        _company(name="SoftCo", industry="Software", location="TX"),
        _person(bound=True, role_relevance=True),
        _intent(needs_estimation="yes", signal="expansion"),
        _timing(window="now"),
    )
    assert rec == "skip"


def test_contact_now_requires_verified_location():
    """R2 regression (the nsarro@unitedcr.com bug): even a high-scoring,
    bound, relevant-role lead with NO verified geolocation can never be
    contact_now — a contractor we cannot locate is not a callable lead. The
    fabricated 'Insurance Restoration Contractor' at an unreadable site had
    location=None and still scored 9.0 contact_now. Now caps at nurture."""
    scorer = LeadScorer()
    _, score, rec = scorer.score(
        "nsarro@unitedcr.com",
        _company(name="United Construction", industry="contractor", location=""),  # no location
        _person(bound=True, role_relevance=True, role="Estimator"),
        _intent(needs_estimation="yes", signal="active bids"),
        _timing(window="now"),
    )
    assert score >= 6.0  # every other signal qualifies
    assert rec == "nurture"  # never contact_now without a location


def test_regate_contact_now_requires_verified_location():
    """R2 applies at read-time re-gating too: a stored dossier with no
    location is demoted from contact_now to nurture."""
    d = _dossier(role="Owner", stored_rec="contact_now", bound=True)
    d.company.location = ""
    assert regate_recommendation(d) == "nurture"


def test_contact_now_requires_relevant_role():
    """High score + bound person but WRONG role (IT/support) never contact_now
    — caps at nurture, not contact_now."""
    scorer = LeadScorer()
    _, score, rec = scorer.score(
        "x@acme.com",
        _company(name="Acme Construction", industry="general contractor", location="Texas"),
        _person(bound=True, role_relevance=False, role="IT Manager"),  # wrong role
        _intent(needs_estimation="yes", signal="active bids"),
        _timing(window="now"),
    )
    assert score >= 6.0
    assert rec == "nurture"  # not contact_now — no relevant decision-maker


def test_backward_compatibility_ai_ask_ignored():
    """ai_ask parameter is accepted but not used (backward compat)."""
    call_count = 0
    def counting_ai_ask(prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        return '{"fit": "test", "potential_score": 5.0, "recommendation": "nurture"}'

    scorer = LeadScorer(ai_ask=counting_ai_ask)
    _, score, rec = scorer.score(
        "x@acme.com",
        _company(name="Acme", industry="construction", location="TX"),
        _person(bound=True, role_relevance=True),
        _intent(needs_estimation="yes", signal="bids"),
        _timing(window="now"),
    )
    # ai_ask should NOT have been called
    assert call_count == 0
    # Score should be deterministic
    assert score >= 6.0
    assert rec == "contact_now"


# --- Re-gate: stored dossiers are re-derived against CURRENT rules ----

def _dossier(email="x@acme.com", *, industry="general contractor", role="Owner",
             stored_rec="contact_now", score=8.0, bound=True, name="Acme Builders"):
    """Build a LeadDossier with an OLD/AI-era stored recommendation."""
    return LeadDossier(
        email=email,
        domain="acme.com",
        company=_company(name=name, industry=industry, location="Texas"),
        person=PersonFindings(name="Jane", role=role, role_relevance=True, bound=bound),
        intent=_intent(),
        timing=_timing(),
        potential_score=score,
        recommendation=stored_rec,
    )


def test_regate_keeps_legit_owner_contact_now():
    """A genuine bound owner at a construction company stays contact_now."""
    d = _dossier(role="Owner", stored_rec="contact_now", bound=True)
    assert regate_recommendation(d) == "contact_now"


def test_regate_demotes_ai_era_sales_representation():
    """Old AI-era contact_now for a Sales Rep / Contact / Employee is DEMOTED —
    the deterministic role list is the authority, not the stored AI guess."""
    for role in (
        "Sales Representative, Ferguson Water Works",
        "Contact / Representative",
        "Employee at Stark Pavement Corporation",
        "Contract Administrator",
        "Estimator",            # does estimation in-house — competitor, not buyer
        "Purchasing Manager",
    ):
        d = _dossier(role=role, stored_rec="contact_now")
        got = regate_recommendation(d)
        assert got != "contact_now", role
        assert got in ("nurture", "skip"), role
        # Score 8 + bound but role irrelevant -> nurture (not skip)
        assert got == "nurture", role


def test_regate_high_score_unbound_stays_nurture():
    d = _dossier(role="Owner", bound=False, stored_rec="contact_now")
    assert regate_recommendation(d) == "nurture"


def test_regate_non_construction_hard_skips():
    """IT/software referral that somehow stored contact_now is hard skipped."""
    d = _dossier(role="Owner", industry="IT Services", stored_rec="contact_now")
    assert regate_recommendation(d) == "skip"


def test_regate_empty_role_never_contact_now():
    d = _dossier(role="", stored_rec="contact_now", bound=True)
    assert regate_recommendation(d) == "nurture"
