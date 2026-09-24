"""LeadScorer — deterministic signal-based scoring tests."""

from __future__ import annotations

from pathlib import Path

from app.lead_research.models import AIEvidence, CompanyProfile, IntentAssessment, LeadDossier, PersonFindings, TimingAssessment
from app.lead_research.scoring import (
    LeadScorer,
    regate_recommendation,
    regate_verdict,
    rescore_dossier,
)


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
        # "Q1" is a quarter label, not a timing window — no timing bonus.
        _timing(window="Q1"),
    )
    # construction(2.0) + role(1.0) + needs_est(2.0) + signal(1.0) + area(0.5) = 6.5
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


# --- the timing sentinel is not an observation ----------------------------
# Fixed 2026-09-21. ``timing.window`` defaults to the STRING "unknown", which
# ``intent_timing`` writes when the AI call raised or returned something
# unparseable. The signal used to be ``if timing.window:`` — a truthiness test
# — so every FAILED timing call was paid the same +0.5 as a real "now". The
# signals below hold everything else equal so the timing contribution is the
# only difference between them.


def _score_with_window(window: str) -> float:
    _, score, _ = LeadScorer().score(
        "x@acme.com",
        _company(name="Acme Construction", industry="general contractor", location=""),
        _person(bound=False, role_relevance=False, name=""),
        _intent(needs_estimation="no", signal=""),
        _timing(window=window),
    )
    return score


def test_a_failed_timing_call_does_not_earn_the_timing_signal():
    """The defect: a failure scoring better than silence would."""
    assert _score_with_window("unknown") == _score_with_window("")


def test_a_real_window_still_earns_it():
    """The counter-case — the allowlist must not abolish the signal."""
    for window in ("now", "soon", "later"):
        assert _score_with_window(window) == _score_with_window("") + 0.5, window


def test_a_non_window_string_does_not_earn_it():
    """Free text that is not a window is not a window.

    The earlier test used ``window="Q1"`` and expected the +0.5 — arbitrary
    text scored, which is the same defect from the other side.
    """
    assert _score_with_window("Q1") == _score_with_window("")


def test_a_window_is_matched_case_insensitively():
    """The AI's JSON is asked for lowercase, but "Now" is the same window."""
    assert _score_with_window(" now ") == _score_with_window("now")


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
             stored_rec="contact_now", score=8.0, bound=True, name="Acme Builders",
             domain="acme.com", fact="", refined_company="", fit="Scored at research time."):
    """Build a LeadDossier with an OLD/AI-era stored recommendation.

    ``fit`` defaults to a MEASURED-looking sentence on purpose: these tests are
    about re-gating a dossier whose score is real, so they must not accidentally
    exercise the abandoned-lead path (see the ``TestAbandonedLeads`` block).
    """
    facts = [AIEvidence(claim=fact, source_url="https://x", source_type="web",
                        confidence="high")] if fact else []
    return LeadDossier(
        email=email,
        domain=domain,
        refined_company=refined_company,
        company=_company(name=name, industry=industry, location="Texas", facts=facts),
        person=PersonFindings(name="Jane", role=role, role_relevance=True, bound=bound),
        intent=_intent(),
        timing=_timing(),
        fit=fit,
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


def test_regate_off_vertical_hard_skips():
    """Fiber/telecom/utility industry passes binary _is_construction but
    is_off_vertical -> hard skip (never burns deep-research credits)."""
    d = _dossier(role="Owner", industry="Fiber Installation", stored_rec="contact_now")
    assert regate_recommendation(d) == "skip"


def test_regate_construction_not_off_vertical_passes():
    """General contractor industry is NOT off-vertical -> normal scoring."""
    d = _dossier(role="Owner", industry="general contractor", stored_rec="contact_now")
    assert regate_recommendation(d) == "contact_now"


def test_regate_non_client_hard_skips():
    """An A/E/C consultancy that somehow stored contact_now is NOT a buyer —
    is_non_client -> hard skip, so old irrelevant dossiers vanish from the
    frontend without re-researching (the 24-dossier live-store audit)."""
    d = _dossier(role="Owner", industry="Engineering Consultancy",
                 stored_rec="contact_now", score=8.0, bound=True)
    assert regate_recommendation(d) == "skip"

    # A trade ASSOCIATION is also not a client (plan service / builders exchange).
    a = _dossier(role="Owner", industry="Builders Exchange / Plan Service",
                 stored_rec="contact_now")
    assert regate_recommendation(a) == "skip"


def test_regate_phrase_precise_keeps_engineering_contractor():
    """'General Engineering Contractor' is NOT flagged: single 'engineering'
    never matches — only the explicit non-client PHRASES ('engineering firm',
    'engineering services', ...) do. The funnel is never starved on a guess."""
    d = _dossier(role="Owner", industry="General Engineering Contractor",
                 stored_rec="contact_now")
    assert regate_recommendation(d) == "contact_now"


# --- Identity backstop: name / domain / fact catch the mislabeled-industry
# --- class (the 2026-09-08 purge: AI stored "General Contractor" for a marine
# --- / heavy-civil / suppliers / AEC-consultant, so every industry gate missed).

def test_regate_name_off_vertical_hard_skips():
    """A company whose NAME names the off-vertical (Signature BRIDGE) hard-skips
    even when the AI mislabeled the industry as 'General Contractor'."""
    d = _dossier(name="Signature Bridge Construction",
                 industry="General Contractor", role="Owner", bound=True)
    assert regate_recommendation(d) == "skip"


def test_regate_name_materials_supplier_hard_skips():
    """'InRoads Paving, Milling and Materials' — identity carries 'materials'."""
    d = _dossier(name="InRoads Paving, Milling and Materials",
                 industry="Specialty Subcontractor", role="Owner", bound=True)
    assert regate_recommendation(d) == "skip"


def test_regate_domain_off_vertical_hard_skips():
    """Email domain is a legit identity string: a marine / utility domain that
    the AI left with a vague industry is caught here."""
    d = _dossier(name="NASSCO East", domain="utlmarinebuilders.com",
                 industry="Specialty Subcontractor", role="Owner", bound=True)
    assert regate_recommendation(d) == "skip"


def test_regate_fact_non_client_hard_skips():
    """When the AI-stored industry says 'General Contractor' but the headline
    fact names a non-client class, the fact-term check hard-skips (ABGI)."""
    d = _dossier(name="ABGI", industry="General Contractor", role="Owner",
                 bound=True,
                 fact="ABGI is a program-management company focused on "
                      "physical brand assets for established retailers")
    assert regate_recommendation(d) == "skip"


def test_regate_marine_industry_hard_skips():
    """New config terms: an industry that names marine/heavy-civil/infrastructure
    construction is off the building-trades vertical (SYB, Dutra, Power Eng)."""
    for ind in (
        "General Contractor — Heavy Civil Construction",
        "Marine & Heavy Civil Construction Contractor",
        "General Contractor — Energy & Alternative Fuel Infrastructure",
    ):
        d = _dossier(name="X Builders", industry=ind, role="Owner", bound=True)
        assert regate_recommendation(d) == "skip", ind


def test_regate_clean_gc_never_overridden_by_fact_words():
    """Conservattive: a legit GC whose fact mentions SUPPLY CHAIN / distribution
    coordination (excluded-list words) is NOT skipped — the excluded list is
    applied to identity strings, never to the fact sentence."""
    d = _dossier(name="Fairway Construction", industry="general contractor",
                 role="Owner", bound=True,
                 fact="Fairway Construction is a commercial general contractor "
                      "providing construction management and supply chain "
                      "coordination to building owners")
    assert regate_recommendation(d) == "contact_now"


def test_regate_clean_gc_geotechnical_trade_not_suppressed():
    """A geotechnical SPECIALTY CONTRACTOR that bids (Schnabel, Nicholson) is
    legitimately in the funnel — the term lists carry no 'geotechnical', so the
    backstop never rejects an earthwork/shoring trade on a word guess."""
    d = _dossier(name="Schnabel", industry="Specialty Subcontractor",
                 role="Owner", bound=True, domain="schnabel.com",
                 fact="Schnabel is a geostructural design-build contractor "
                      "specializing in earth retention and deep foundations")
    assert regate_recommendation(d) == "contact_now"


# --- Read-time re-score: the score is re-derived, not just the verdict ------
# Added 2026-09-21. ``regate_recommendation`` always re-applied TODAY'S gates
# at read time, but compared them against the STORED score and left that score
# on screen. So a formula correction reached the gate and never reached the
# number the gate compares — a dossier sitting on the 6.0 line thanks to a
# bonus the formula no longer grants was still served contact_now, with the
# number that justified it.
#
# The fix is ``rescore_dossier``, which reuses the SAME LeadScorer the research
# path uses (CLAUDE.md §14 — one formula, not two).


def test_rescore_re_derives_the_stored_score():
    """The 8.0 the row was stored with is not what the signals say today."""
    d = _dossier(score=8.0)
    assert rescore_dossier(d) == 8.5  # 2.0+1.5+1.0+2.0+1.0+0.5+0.5


def test_rescore_reproduces_a_research_time_score():
    """The counter-case: on unchanged inputs it must agree with Stage 4.

    Recomputing the score the scorer itself just produced has to be a no-op,
    or the leads page and a fresh research of the same lead would disagree.
    """
    d = _dossier(score=0.0)
    _, score, _ = LeadScorer().score(
        d.email, d.company, d.person, d.intent, d.timing,
    )
    assert rescore_dossier(d) == score


def test_rescore_honours_the_timing_sentinel_end_to_end():
    """The stored score paid +0.5 for a timing call that never answered.

    ``_dossier``'s signals total 8.5 including a real "now" window. Swap in
    the FAILURE sentinel and the 0.5 must go; a real window keeps it.
    """
    paid = _dossier(score=6.5, stored_rec="contact_now")
    paid.timing = TimingAssessment(window="unknown")   # the failure sentinel
    real = _dossier(score=6.5, stored_rec="contact_now")
    real.timing = TimingAssessment(window="soon")      # a real observation
    assert rescore_dossier(paid) == 8.0
    assert rescore_dossier(real) == 8.5
    assert rescore_dossier(paid) == rescore_dossier(real) - 0.5


def test_the_gate_uses_the_re_derived_score():
    """A row kept at contact_now by the bogus bonus drops to nurture.

    The stored 6.0 includes the +0.5 the old formula paid a FAILED timing
    call. Under today's formula the same signals total 5.5 — under the
    contact_now line — so re-deriving the score is not cosmetic, it changes
    the verdict. That is the shape of the live defect: 27 stored dossiers sat
    on a threshold the bonus carried, and 6 changed class.
    """
    d = _dossier(score=6.0, stored_rec="contact_now",
                 fact="Acme Builders is a commercial general contractor.")
    d.intent = IntentAssessment(needs_estimation="no", signal="")
    d.timing = TimingAssessment(window="unknown")
    # construction(2.0) + bound(1.5) + role(1.0) + facts(0.5) + area(0.5)
    assert rescore_dossier(d) == 5.5
    assert regate_verdict(d) == ("nurture", 5.5)


# --- Abandoned leads have no score to re-derive -----------------------------
# ``potential_score`` defaults to 0.0 and only Stage 4 overwrites it, so every
# gate that returns BEFORE Stage 4 — junk domain, free mail, dead domain, client
# fit — persists 0.0 for a lead whose research never ran. Re-deriving a score
# for such a row computes a number from fields that were never researched.
#
# Measured on the live store 2026-09-21: 397 of 1179 rows are abandoned this
# way, and re-scoring them manufactured a PASSING score for 43 companies the
# pipeline had explicitly decided are not our clients.


def test_an_abandoned_lead_is_not_given_an_invented_score():
    """A 'Not our client' row keeps its 0.0 instead of getting a made-up 3.5."""
    d = _dossier(stored_rec="skip", score=0.0,
                 fit="Not our client — Acme is a software company.")
    assert rescore_dossier(d) == 0.0


def test_an_abandoned_lead_keeps_its_skip_verdict():
    """The regression this guard exists for.

    Under today's signals this row would compute 8.5 and flip to contact_now —
    but the pipeline already decided this company is not a client, and no
    research backs the 8.5. It must stay skip.
    """
    d = _dossier(stored_rec="skip", score=0.0,
                 fit="Not our client — Acme is a software company.")
    rec, score = regate_verdict(d)
    assert (rec, score) == ("skip", 0.0)


def test_every_pre_scoring_gate_prefix_is_registered():
    """A future early-return gate must register its ``fit`` prefix.

    ``_ABANDONED_FIT_PREFIXES`` is how an abandoned lead is told apart from a
    scored one. A new gate that writes an unregistered sentence would make
    ``rescore_dossier`` score a lead that was never researched — the 43-lead
    defect again, silently. This reads the gate sites out of ``agent.py`` so
    the failure lands here, with the prefix to add, instead of in production.
    """
    import re

    from app.lead_research.scoring import _ABANDONED_FIT_PREFIXES

    src = (
        Path(__file__).resolve().parents[2] / "app" / "lead_research" / "agent.py"
    ).read_text(encoding="utf-8")
    # The literal part of every `dossier.fit = ...` that assigns a STRING — an
    # f-string is captured up to its first `{`, and a wrapped `= (` form is
    # allowed. `dossier.fit = fit` (Stage 4 handing over the scorer's own
    # sentence) has no quote and is deliberately not matched.
    assignments = len(re.findall(r"dossier\.fit\s*=", src))
    literals = re.findall(r'dossier\.fit\s*=\s*\(?\s*f?["\']([^"\'{]*)', src)
    assert literals, "the scan found no gate sentences — did agent.py move?"
    assert len(literals) == assignments - 1, (
        f"{assignments} `dossier.fit =` sites but only {len(literals)} parsed "
        "as gate sentences — a gate is written in a form this scan misses, so "
        "this guard is no longer complete"
    )
    unregistered = [
        s for s in literals
        if not s.startswith(_ABANDONED_FIT_PREFIXES)
    ]
    assert not unregistered, (
        "these pre-scoring gate sentences are not registered in "
        "_ABANDONED_FIT_PREFIXES, so their leads would be re-scored as if "
        f"researched: {unregistered}"
    )
