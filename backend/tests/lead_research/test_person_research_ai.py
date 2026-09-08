"""PersonResearcherAI — deterministic tests with injected seams."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.lead_research.person_research_ai import PersonResearcherAI, _parse_ai_json
from app.person_research.models import AttributionVerdict, PersonCandidate, ResearchEvidence, ResearchResult
from tests.lead_research.conftest import make_fake_ai, make_fake_fetch, make_fake_search


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_det_result(verdict, name="", role="", bound=False, evidence=None):
    """Build a fake ResearchResult for the deterministic seam."""
    candidates = []
    if name:
        candidates.append(PersonCandidate(
            name=name,
            role=role,
            bound=bound,
            evidence=evidence or [],
        ))
    return ResearchResult(
        email="test@test.com",
        email_hash="abc123",
        domain="test.com",
        verdict=verdict,
        candidates=candidates,
    )


def _good_ai_person_response():
    return {
        "person_name": "Jane Doe",
        "person_role": "Project Manager",
        "role_relevance": True,
        "bound": True,
        "evidence": [
            {"claim": "Listed on team page", "source_url": "https://acme.com/team", "source_type": "website", "confidence": "verified"},
        ],
    }


# ---------------------------------------------------------------------------
# Deterministic path
# ---------------------------------------------------------------------------

def test_deterministic_attributed_skips_ai():
    """When deterministic returns attributed → AI is never called."""
    ai_called = []

    det_result = _make_det_result(
        AttributionVerdict.attributed,
        name="John Smith",
        role="Estimator",
        bound=True,
        evidence=[ResearchEvidence(
            source_url="https://acme.com/about",
            source_type="company_site",
            authority="authoritative",
            evidence_kind="email_present",
        )],
    )

    def fake_det(email, domain, **kw):
        return det_result

    researcher = PersonResearcherAI(
        deterministic=fake_det,
        ai_ask=lambda p: (ai_called.append(1), '{"person_name":"should not happen"}')[1],
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("john@acme.com", "acme.com", company_name="Acme")
    assert result.name == "John Smith"
    assert result.role == "Estimator"
    assert result.bound is True
    assert len(ai_called) == 0  # AI was NOT called


def test_deterministic_linkedin_rejected_on_mismatch():
    """R1 guard applies on the deterministic path too: a LinkedIn URL in the
    deterministic evidence that does NOT belong to the attributed candidate is
    rejected (honest empty over another person's profile)."""
    det_result = _make_det_result(
        AttributionVerdict.attributed,
        name="John Smith",
        role="Estimator",
        bound=True,
        evidence=[ResearchEvidence(
            source_url="https://www.linkedin.com/in/jane-doe-123",
            source_type="company_site",
            authority="authoritative",
            evidence_kind="email_present",
        )],
    )

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: det_result,
        ai_ask=lambda p: '{"person_name":"should not happen"}',
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("john@acme.com", "acme.com")
    assert result.name == "John Smith"
    assert result.bound is True
    assert result.linkedin == ""  # Jane Doe's profile is not John Smith's


def test_deterministic_unattributed_triggers_ai():
    """When deterministic returns unattributed → AI augments."""
    det_result = _make_det_result(AttributionVerdict.unattributed)

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: det_result,
        ai_ask=make_fake_ai(_good_ai_person_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("jane@acme.com", "acme.com", company_name="Acme")
    assert result.name == "Jane Doe"
    assert result.bound is True


def test_deterministic_candidates_found_triggers_ai():
    """candidates_found (not attributed) → AI augments."""
    det_result = _make_det_result(
        AttributionVerdict.candidates_found,
        name="Someone",
        role="",
        bound=False,
    )

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: det_result,
        ai_ask=make_fake_ai(_good_ai_person_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("jane@acme.com", "acme.com")
    assert result.name == "Jane Doe"  # AI result, not deterministic


def test_no_deterministic_goes_directly_to_ai():
    """No deterministic fn provided → AI is called directly."""
    researcher = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai(_good_ai_person_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("jane@acme.com", "acme.com")
    assert result.name == "Jane Doe"


def test_deterministic_exception_falls_back_to_ai():
    """Deterministic throws → falls back to AI."""
    def exploding_det(email, domain, **kw):
        raise RuntimeError("DB down")

    researcher = PersonResearcherAI(
        deterministic=exploding_det,
        ai_ask=make_fake_ai(_good_ai_person_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("jane@acme.com", "acme.com")
    assert result.name == "Jane Doe"


def test_company_facts_passed_into_prompt():
    """Verified Stage 1 company facts are fed to the AI prompt for binding."""
    from app.lead_research.models import AIEvidence

    captured = {}

    def capturing_ai(prompt: str) -> str:
        captured["prompt"] = prompt
        return '{"person_name": "Mike Dretzka", "person_role": "Vice President", "role_relevance": true, "bound": true, "evidence": [{"claim": "Mike Dretzka is VP (verified fact)", "source_url": "https://upiunderground.com/about", "source_type": "about_page", "confidence": "verified", "source_note": "About us"}]}'

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=capturing_ai,
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    facts = [
        AIEvidence(claim="Mike Dretzka is the Vice President", source_url="https://upiunderground.com/about", source_type="about_page", confidence="verified"),
        AIEvidence(claim="Unverified guess", source_url="", source_type="inferred", confidence="unverified"),
    ]
    result = researcher.research(
        "jdretzka@upi.com", "upi.com", company_name="UPI", company_facts=facts,
    )
    # Verified fact with source_url is surfaced in the prompt
    assert "Mike Dretzka is the Vice President" in captured["prompt"]
    assert "https://upiunderground.com/about" in captured["prompt"]
    # Unverified fact with no source_url is NOT fed to the AI
    assert "Unverified guess" not in captured["prompt"]
    assert result.name == "Mike Dretzka"
    assert result.bound is True


def test_person_bare_root_binding_unbound_by_guard():
    """Exact-page guard on the person path: a 'bound' person whose only evidence
    is a bare-root 'verified' fact is demoted (fact -> unverified) and the
    binding cannot survive the verified-source requirement."""
    response = {
        "person_name": "Fake Person",
        "person_role": "Estimator",
        "role_relevance": True,
        "bound": True,
        "evidence": [
            {"claim": "Listed on company site", "source_url": "https://upiunderground.com", "source_type": "website", "confidence": "verified"},
        ],
    }
    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(response),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("x@upi.com", "upi.com")
    assert result.evidence[0].confidence == "unverified"
    assert result.evidence[0].source_note == "source location not reported"
    assert result.bound is False  # bare root can no longer carry a binding


def test_person_gather_site_labels_pages_with_real_urls():
    """The exact-page fix on the person path too — team/about pages arrive
    labeled with their real URLs, never anonymous text."""
    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(_good_ai_person_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    content = researcher._gather_site(make_fake_fetch({
        "https://acme.com": "<html>Acme Construction</html>",
        "https://acme.com/about": "<html>About Acme</html>",
        "https://acme.com/contact": "<html>Contact</html>",
        "https://acme.com/team": "<html>Team</html>",
    }), "acme.com")
    assert "[PAGE: Homepage — https://acme.com]" in content
    assert "[PAGE: About us — https://acme.com/about]" in content
    assert "[PAGE: Team page — https://acme.com/team]" in content


def test_linkedin_profile_enrichment_appends_cited_facts(monkeypatch):
    """Phase E person enrichment: after the R1 name-match pick, the person's
    own public LinkedIn profile is read and its facts appended as LinkedIn-cited
    evidence (source_url = the exact profile URL)."""
    monkeypatch.delenv("LINKEDIN_LANE_ENABLED", raising=False)
    calls = {"n": 0}
    profile_url = "https://www.linkedin.com/in/jane-doe-123"

    def seq_ai(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({
                "person_name": "Jane Doe",
                "person_role": "VP Estimating",
                "role_relevance": True,
                "bound": True,
                "evidence": [
                    {"claim": "Listed on team page", "source_url": "https://acme.com/team", "source_type": "team_page", "confidence": "verified", "source_note": "Team page"},
                ],
            })
        return json.dumps({
            "facts": [
                {"claim": "VP Estimating at Acme (profile)", "source_url": profile_url, "source_type": "linkedin", "confidence": "verified", "source_note": "LinkedIn profile"},
            ]
        })

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=seq_ai,
        search=make_fake_search([{"url": profile_url, "title": "Jane", "snippet": ""}]),
        fetch_page=make_fake_fetch(),
        linkedin_extract=lambda url: "Jane Doe — VP Estimating at Acme Construction, Dallas, TX.",
    )
    result = researcher.research("jane@acme.com", "acme.com", company_name="Acme")
    assert calls["n"] == 2  # main pass + profile enrichment pass
    assert result.linkedin == profile_url
    assert any(e.source_url == profile_url and e.source_type == "linkedin" for e in result.evidence)


def test_linkedin_profile_enrichment_skipped_when_walled(monkeypatch):
    """A login-walled / extract-less profile contributes nothing: only the main
    pass runs, no LinkedIn evidence is appended (honest fallback)."""
    monkeypatch.delenv("LINKEDIN_LANE_ENABLED", raising=False)
    calls = {"n": 0}
    profile_url = "https://www.linkedin.com/in/jane-doe-123"

    def seq_ai(prompt: str) -> str:
        calls["n"] += 1
        return json.dumps(_good_ai_person_response())

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=seq_ai,
        search=make_fake_search([{"url": profile_url, "title": "Jane", "snippet": ""}]),
        fetch_page=make_fake_fetch(),
        linkedin_extract=lambda url: "",  # walled
    )
    result = researcher.research("jane@acme.com", "acme.com", company_name="Acme")
    assert calls["n"] == 1  # no second pass
    assert not any(e.source_type == "linkedin" for e in result.evidence)


def test_linkedin_profile_enrichment_drops_social_noise(monkeypatch):
    """The enrich lane reports social-profile junk (connections/followers/
    education/certs/languages) — the deterministic relevance filter drops it
    before it can persist, keeping only business facts (Phase 2A)."""
    monkeypatch.delenv("LINKEDIN_LANE_ENABLED", raising=False)
    calls = {"n": 0}
    profile_url = "https://www.linkedin.com/in/jane-doe-123"

    def seq_ai(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps(_good_ai_person_response())
        return json.dumps({
            "facts": [
                {"claim": "VP Estimating at Acme (profile)", "source_url": profile_url, "source_type": "linkedin", "confidence": "verified", "source_note": "LinkedIn profile"},
                {"claim": "Jane Doe has 500+ connections on LinkedIn", "source_url": profile_url, "source_type": "linkedin", "confidence": "verified", "source_note": "LinkedIn profile"},
                {"claim": "Attended Louisiana State University", "source_url": profile_url, "source_type": "linkedin", "confidence": "verified", "source_note": "LinkedIn profile"},
                {"claim": "Holds CITI certifications", "source_url": profile_url, "source_type": "linkedin", "confidence": "verified", "source_note": "LinkedIn profile"},
            ]
        })

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=seq_ai,
        search=make_fake_search([{"url": profile_url, "title": "Jane", "snippet": ""}]),
        fetch_page=make_fake_fetch(),
        linkedin_extract=lambda url: "Jane Doe — VP Estimating at Acme Construction, Dallas, TX.",
    )
    result = researcher.research("jane@acme.com", "acme.com", company_name="Acme")
    li_facts = [e.claim for e in result.evidence if e.source_type == "linkedin"]
    assert li_facts == ["VP Estimating at Acme (profile)"]


def test_source_note_roundtrip_from_ai():
    """source_note from the AI response survives into the parsed person fact."""
    response = {
        "person_name": "Jane Doe",
        "person_role": "Manager",
        "role_relevance": True,
        "bound": False,
        "evidence": [
            {"claim": "Contact listed on page", "source_url": "https://acme.com/contact", "source_type": "contact_page", "confidence": "verified", "source_note": "Contact page"},
        ],
    }
    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(response),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("x@acme.com", "acme.com")
    assert result.evidence[0].source_note == "Contact page"


# ---------------------------------------------------------------------------
# AI safety rules
# ---------------------------------------------------------------------------

def test_ai_bound_without_source_downgraded():
    """AI claims bound=True but no source_url → downgraded to unbound."""
    bad_response = {
        "person_name": "Fake Person",
        "person_role": "Manager",
        "role_relevance": True,
        "bound": True,
        "evidence": [
            {"claim": "Some claim", "source_url": "", "source_type": "", "confidence": "unverified"},
        ],
    }

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(bad_response),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("x@acme.com", "acme.com")
    assert result.name == "Fake Person"
    assert result.bound is False  # downgraded!


def test_ai_no_evidence_keeps_unbound():
    """AI returns empty evidence → unbound."""
    response = {
        "person_name": "",
        "person_role": "",
        "role_relevance": False,
        "bound": False,
        "evidence": [],
    }

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(response),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("x@acme.com", "acme.com")
    assert result.name == ""
    assert result.bound is False


def test_ai_extracts_linkedin_from_search_results():
    """A ``linkedin.com/in`` URL surfaced in the search results is stored as
    the person's ``linkedin`` field — but ONLY when the handle belongs to the
    person (R1: name-match guard, honest capture, never another person's
    profile)."""
    response = {
        "person_name": "Jane Doe",
        "person_role": "Manager",
        "role_relevance": True,
        "bound": False,
        "evidence": [],
    }
    search_results = [
        {"url": "https://www.linkedin.com/in/jane-doe-123", "title": "Jane", "snippet": ""},
        {"url": "https://example.com/contact", "title": "Contact", "snippet": ""},
    ]
    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(response),
        search=make_fake_search(search_results),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("x@acme.com", "acme.com", company_name="Acme")
    assert result.linkedin == "https://www.linkedin.com/in/jane-doe-123"
    assert result.linkedin in result.to_dict()["linkedin"]


def test_linkedin_rejected_when_handle_mismatches_person():
    """R1 regression (the nsarro@unitedcr.com bug): a ``linkedin.com/in`` URL
    found on the company's search results is REJECTED when the handle does NOT
    belong to the researched person. Michael Hammond's profile must never be
    credited to Nick Sarro — an honest empty is better than a wrong link."""
    response = {
        "person_name": "Nick Sarro",
        "person_role": "Estimator",
        "role_relevance": True,
        "bound": False,
        "evidence": [],
    }
    search_results = [
        {"url": "https://au.linkedin.com/in/michael-hammond-20b38742", "title": "Michael Hammond", "snippet": ""},
    ]
    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(response),
        search=make_fake_search(search_results),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("nsarro@unitedcr.com", "unitedcr.com", company_name="United Construction")
    assert result.linkedin == ""


def test_linkedin_accepted_when_handle_shares_surname():
    """A handle that shares the person's surname (no given-name overlap) is
    still accepted — e.g. a ``sarro`` slug for Nick Sarro."""
    response = {
        "person_name": "Nick Sarro",
        "person_role": "Estimator",
        "role_relevance": True,
        "bound": False,
        "evidence": [],
    }
    search_results = [
        {"url": "https://www.linkedin.com/in/sarro-n", "title": "Nick", "snippet": ""},
    ]
    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(response),
        search=make_fake_search(search_results),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("nsarro@unitedcr.com", "unitedcr.com", company_name="United Construction")
    assert result.linkedin == "https://www.linkedin.com/in/sarro-n"


def test_linkedin_ignores_company_page():
    """A ``linkedin.com/company`` URL is not a person profile and must NOT be
    stored as the decision-maker's ``linkedin``."""
    response = {
        "person_name": "Fake Person",
        "person_role": "Manager",
        "role_relevance": True,
        "bound": False,
        "evidence": [],
    }
    search_results = [
        {"url": "https://www.linkedin.com/company/acme", "title": "Acme", "snippet": ""},
    ]
    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(response),
        search=make_fake_search(search_results),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("x@acme.com", "acme.com", company_name="Acme")
    assert result.linkedin == ""


def test_ai_garbage_response():
    """AI returns non-JSON → partial PersonFindings with error."""
    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=lambda p: "not json at all",
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("x@acme.com", "acme.com")
    assert result.name == ""
    assert result.bound is False
    assert len(result.evidence) == 1
    assert "unparseable" in result.evidence[0].claim.lower()


def test_ai_raises_exception():
    """AI call throws → partial PersonFindings with error."""
    def boom(p):
        raise RuntimeError("API down")

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=boom,
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("x@acme.com", "acme.com")
    assert result.name == ""
    assert "failed" in result.evidence[0].claim.lower()


# ---------------------------------------------------------------------------
# Search + fetch integration
# ---------------------------------------------------------------------------

def test_gathers_multiple_search_queries():
    """Multiple search queries are run and deduplicated."""
    seen = []

    def tracking_search(query):
        seen.append(query)
        return []

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(_good_ai_person_response()),
        search=tracking_search,
        fetch_page=make_fake_fetch(),
    )
    researcher.research("jane@acme.com", "acme.com")
    assert len(seen) == 2  # email + linkedin (team/about-contact dorks moved to the free crawl)


def test_fetches_team_pages():
    """Fetches homepage + team-related pages."""
    fetched_urls = []

    def tracking_fetch(url):
        fetched_urls.append(url)
        return SimpleNamespace(ok=True, html="<html>team page</html>")

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(_good_ai_person_response()),
        search=make_fake_search(),
        fetch_page=tracking_fetch,
    )
    researcher.research("jane@acme.com", "acme.com")
    assert "https://acme.com" in fetched_urls
    assert "https://acme.com/team" in fetched_urls
    assert "https://acme.com/our-team" in fetched_urls


def test_fetch_failure_doesnt_crash():
    """Fetch errors are silently skipped."""
    def fail_fetch(url):
        raise ConnectionError("timeout")

    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(_good_ai_person_response()),
        search=make_fake_search(),
        fetch_page=fail_fetch,
    )
    result = researcher.research("jane@acme.com", "acme.com")
    assert result.name == "Jane Doe"


def test_empty_search_results():
    """Empty search → still works."""
    researcher = PersonResearcherAI(
        deterministic=lambda e, d, **kw: _make_det_result(AttributionVerdict.unattributed),
        ai_ask=make_fake_ai(_good_ai_person_response()),
        search=make_fake_search([]),
        fetch_page=make_fake_fetch(),
    )
    result = researcher.research("jane@acme.com", "acme.com")
    assert result.name == "Jane Doe"


# ---------------------------------------------------------------------------
# _parse_ai_json (duplicated here for completeness — same as company_research)
# ---------------------------------------------------------------------------

def test_parse_json_with_fences():
    raw = '```json\n{"key": "value"}\n```'
    assert _parse_ai_json(raw) == {"key": "value"}


def test_parse_json_garbage():
    assert _parse_ai_json("not json") == {}
