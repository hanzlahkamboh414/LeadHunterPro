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
    assert len(seen) == 3  # email, domain team, domain about contact


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
