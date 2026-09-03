"""WebsiteSource + IndexedWebSource — deterministic, injected fetchers."""

from __future__ import annotations

from app.person_research.sources import (
    IndexedWebSource,
    WebsiteSource,
)
from tests.person_research.conftest import homepage, make_fetch, team_page


def test_website_source_binds_region_email(acme_pages):
    src = WebsiteSource(fetch_page=make_fetch(acme_pages))
    facts = src.discover("acmebuild.com", "johnsmith@acmebuild.com")
    assert "https://acmebuild.com" in facts.pages
    ctxs = facts.email_contexts.get("johnsmith@acmebuild.com", [])
    assert any(c["name"] == "John Smith" for c in ctxs)
    assert any(c["role"] == "Project Manager" for c in ctxs)


def test_website_source_honors_robots(acme_pages):
    pages = dict(acme_pages)
    pages["https://acmebuild.com/robots.txt"] = "User-agent: *\nDisallow: /team\n"
    src = WebsiteSource(fetch_page=make_fetch(pages))
    facts = src.discover("acmebuild.com", "johnsmith@acmebuild.com")
    assert "https://acmebuild.com/team" not in facts.pages


def test_website_source_no_fetch_when_homepage_missing():
    src = WebsiteSource(fetch_page=make_fetch({}))
    facts = src.discover("acmebuild.com", "x@acmebuild.com")
    assert facts.pages == []


def test_website_source_page_cap():
    pages = {
        "https://acmebuild.com": homepage(*[(f"/page{i}", "Team") for i in range(10)]),
    }
    src = WebsiteSource(fetch_page=make_fetch(pages), max_pages=3)
    facts = src.discover("acmebuild.com", "x@acmebuild.com")
    assert len(facts.pages) <= 3


def test_indexed_web_corroborates_email_and_name():
    def fake_search(query):
        if "johnsmith@acmebuild.com" in query:
            return [
                {"url": "https://dir.example.com/acme", "snippet": "John Smith - johnsmith@acmebuild.com Project Manager"},
            ]
        return []

    src = IndexedWebSource(search=fake_search)
    evidence = src.corroborate("johnsmith@acmebuild.com", "acmebuild.com", "John Smith")
    assert len(evidence) == 1
    assert evidence[0].source_type == "indexed_web"
    assert evidence[0].authority == "supporting"


def test_indexed_web_ignores_mismatch():
    def fake_search(query):
        return [{"url": "https://dir.example.com/x", "snippet": "Jane Doe works at Acme"}]

    src = IndexedWebSource(search=fake_search)
    evidence = src.corroborate("johnsmith@acmebuild.com", "acmebuild.com", "John Smith")
    assert evidence == []


def test_indexed_web_empty_on_bad_email():
    src = IndexedWebSource(search=lambda q: [])
    assert src.corroborate("not-an-email", "acme.com", "John Smith") == []
