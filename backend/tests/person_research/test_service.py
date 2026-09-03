"""ResearchService — research + persist, rescore (no external calls), apply."""

from __future__ import annotations

from app.person_research.models import AttributionVerdict, ResearchStatus
from app.person_research.orchestrator import ResearchOrchestrator
from app.person_research.service import ResearchService
from app.person_research.sources import IndexedWebSource, WebsiteSource
from app.person_research.store import ResearchStore
from tests.person_research.conftest import make_fetch


def _service(tmp_path, acme_pages, search=None):
    store = ResearchStore(tmp_path / "pr.sqlite3")
    website = WebsiteSource(fetch_page=make_fetch(acme_pages))
    indexed = IndexedWebSource(search=search or (lambda q: []))
    orch = ResearchOrchestrator(website_source=website, indexed_source=indexed)
    return ResearchService(store, orchestrator=orch)


def test_research_persists_job(tmp_path, acme_pages):
    svc = _service(tmp_path, acme_pages)
    r = svc.research("johnsmith@acmebuild.com", "acmebuild.com")
    assert r.verdict is AttributionVerdict.attributed
    job = svc.store.get_job(r.email_hash)
    assert job["research_status"] == ResearchStatus.completed.value
    assert job["verdict"] == AttributionVerdict.attributed.value


def test_rescore_from_stored_evidence(tmp_path, acme_pages):
    svc = _service(tmp_path, acme_pages)
    svc.research("johnsmith@acmebuild.com", "acmebuild.com")

    # Re-score WITHOUT any external calls (website/search not consulted again).
    r2 = svc.rescore("johnsmith@acmebuild.com")
    assert r2 is not None
    assert r2.verdict is AttributionVerdict.attributed
    assert r2.bound_candidate is not None
    assert r2.bound_candidate.name == "John Smith"


def test_rescore_unknown_email_returns_none(tmp_path):
    svc = _service(tmp_path, {})
    assert svc.rescore("nobody@nowhere.com") is None


def test_apply_to_lead_from_stored(tmp_path, acme_pages):
    svc = _service(tmp_path, acme_pages)
    svc.research("johnsmith@acmebuild.com", "acmebuild.com")
    lead = {
        "plan_holder": {
            "person": None,
            "emails": [{"email": "johnsmith@acmebuild.com", "tier": "format"}],
        }
    }
    out = svc.apply_to_lead(lead, "johnsmith@acmebuild.com")
    assert out["plan_holder"]["person"]["name"] == "John Smith"
    assert out["plan_holder"]["emails"][0]["tier"] == "person_bound"


def test_apply_untouched_when_not_attributed(tmp_path, acme_pages):
    svc = _service(tmp_path, acme_pages)
    svc.research("office@acmebuild.com", "acmebuild.com")
    lead = {"plan_holder": {"person": None, "emails": []}}
    assert svc.apply_to_lead(lead, "office@acmebuild.com") is lead
