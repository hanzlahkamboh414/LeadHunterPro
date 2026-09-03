"""ResearchOrchestrator — Stages 0-2 + ResearchApplier integration boundary."""

from __future__ import annotations

from app.person_research.models import AttributionVerdict
from app.person_research.orchestrator import ResearchApplier, ResearchOrchestrator
from app.person_research.sources import IndexedWebSource, WebsiteSource
from tests.person_research.conftest import make_fetch


def _orch(acme_pages, search=None):
    website = WebsiteSource(fetch_page=make_fetch(acme_pages))
    indexed = IndexedWebSource(search=search or (lambda q: []))
    return ResearchOrchestrator(website_source=website, indexed_source=indexed)


def test_triage_free_mail():
    o = ResearchOrchestrator()
    r = o.research("john@gmail.com", "gmail.com")
    assert r.verdict is AttributionVerdict.unattributed
    assert "triage" in r.source_errors
    assert r.source_errors["triage"] == "free_mail_domain"


def test_triage_generic_local_part(acme_pages):
    o = _orch(acme_pages)
    r = o.research("info@acmebuild.com", "acmebuild.com")
    assert r.verdict is AttributionVerdict.unattributed
    assert r.source_errors["triage"] == "generic_local_part"


def test_full_pipeline_attributed(acme_pages):
    def search(q):
        return [{"url": "https://dir.example.com/acme", "snippet": "John Smith - johnsmith@acmebuild.com"}]

    o = _orch(acme_pages, search=search)
    r = o.research("johnsmith@acmebuild.com", "acmebuild.com")
    assert r.verdict is AttributionVerdict.attributed
    assert r.bound_candidate is not None
    assert r.bound_candidate.name == "John Smith"
    assert r.bound_candidate.corroboration_count >= 1


def test_never_invent_name_from_local_part(acme_pages):
    # johnsmith@ present on site but with NO co-occurring name -> unattributed.
    o = _orch(acme_pages)
    r = o.research("johnsmith@acmebuild.com", "acmebuild.com")
    # With the team card it IS attributed; here we assert we never fabricate
    # when there is no candidate (empty site).
    empty = ResearchOrchestrator(website_source=WebsiteSource(fetch_page=make_fetch({})))
    r2 = empty.research("johnsmith@acmebuild.com", "acmebuild.com")
    assert r2.verdict is AttributionVerdict.unattributed
    assert r2.bound_candidate is None


def test_applier_only_writes_when_attributed(acme_pages):
    o = _orch(acme_pages)
    lead = {
        "company_name": "ACME Build",
        "plan_holder": {
            "person": None,
            "emails": [
                {"email": "johnsmith@acmebuild.com", "tier": "format", "source_url": "x"},
            ],
        },
    }

    # Unattributed -> untouched.
    r_na = o.research("office@acmebuild.com", "acmebuild.com")
    assert ResearchApplier().apply(lead, r_na) is lead

    # Attributed -> person + person_bound tier.
    r_ok = o.research("johnsmith@acmebuild.com", "acmebuild.com")
    out = ResearchApplier().apply(lead, r_ok)
    assert out["plan_holder"]["person"]["name"] == "John Smith"
    assert out["plan_holder"]["emails"][0]["tier"] == "person_bound"
