"""ResearchStore — SQLite jobs + caches + append-only evidence."""

from __future__ import annotations

from app.person_research.models import (
    AttributionVerdict,
    PersonCandidate,
    ResearchEvidence,
    ResearchResult,
    ResearchStatus,
)
from app.person_research.store import ResearchStore, email_hash, normalize_email


def _store(tmp_path):
    return ResearchStore(tmp_path / "research.sqlite3")


def test_normalize_email_lowercases_domain_and_strips_mailto():
    assert normalize_email("John@ACME.com") == "John@acme.com"
    assert normalize_email("mailto:john@acme.com") == "john@acme.com"
    assert normalize_email("not-an-email") == ""


def test_email_hash_stable_across_domain_case():
    # normalize_email lowercases the domain and strips trailing dots but
    # preserves the (RFC case-sensitive) local part, so hashes must agree on
    # domain case and trailing dot only.
    assert email_hash("John@acme.com") == email_hash("John@ACME.com")
    assert email_hash("John@acme.com") == email_hash("John@acme.com.")


def test_create_job_and_requeue(tmp_path):
    s = _store(tmp_path)
    eh = s.create_job("info@acme.com", "acme.com")
    assert s.create_job("info@acme.com", "acme.com") == eh  # idempotent
    job = s.get_job(eh)
    assert job["research_status"] == ResearchStatus.pending.value
    assert s.requeue(eh) is True
    assert s.requeue("nope") is False


def test_recover_interrupted_returns_pending(tmp_path):
    s = _store(tmp_path)
    eh = s.create_job("info@acme.com", "acme.com")
    s.mark_researching(eh)
    s.recover_interrupted()
    assert s.get_job(eh)["research_status"] == ResearchStatus.pending.value


def test_complete_job_append_only_evidence(tmp_path):
    s = _store(tmp_path)
    eh = s.create_job("johnsmith@acme.com", "acme.com")
    result = ResearchResult(
        email="johnsmith@acme.com",
        email_hash=eh,
        domain="acme.com",
        verdict=AttributionVerdict.attributed,
        candidates=[
            PersonCandidate(
                name="John Smith",
                bound=True,
                evidence=[ResearchEvidence(source_url="https://acme.com/team", source_type="company_site", authority="authoritative", evidence_kind="name_co_occurrence")],
            )
        ],
    )
    s.complete_job(result, ResearchStatus.completed)
    # Re-run with a NEW corroboration piece appended.
    result2 = ResearchResult(
        email="johnsmith@acme.com",
        email_hash=eh,
        domain="acme.com",
        verdict=AttributionVerdict.attributed,
        candidates=[
            PersonCandidate(
                name="John Smith",
                bound=True,
                evidence=[
                    ResearchEvidence(source_url="https://acme.com/team", source_type="company_site", authority="authoritative", evidence_kind="name_co_occurrence"),
                    ResearchEvidence(source_url="https://dir.example.com/x", source_type="indexed_web", authority="supporting", evidence_kind="corroboration", snippet="s"),
                ],
            )
        ],
    )
    s.complete_job(result2, ResearchStatus.completed)
    job = s.get_job(eh)
    import json

    evidence = json.loads(job["evidence_json"])
    kinds = {e["evidence_kind"] for e in evidence}
    assert kinds == {"name_co_occurrence", "corroboration"}


def test_domain_cache_respects_version(tmp_path):
    s = _store(tmp_path)
    s.set_domain_facts("acme.com", {"pages": ["x"]}, research_version=1)
    assert s.get_domain_facts("acme.com", 1) == {"pages": ["x"]}
    assert s.get_domain_facts("acme.com", 2) is None  # different research version


def test_query_cache_roundtrip(tmp_path):
    s = _store(tmp_path)
    assert s.get_query_cache("q") is None
    s.set_query_cache("q", [{"url": "u", "snippet": "s"}])
    assert s.get_query_cache("q") == [{"url": "u", "snippet": "s"}]
