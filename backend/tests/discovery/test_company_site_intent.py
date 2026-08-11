"""Offline tests for the company_site intent plugin (Increment 5).

The plugin is fed saved real-HTML fixtures through a stub
:class:`PageFetcher` — no live network (CLAUDE.md §1). Each test pins one
honest outcome: a matched signal becomes traceable ``IntentEvidence``
with the exact page URL; a fetched-but-quiet site is ``EMPTY``; a site
that cannot be reached is ``UNAVAILABLE`` — the two must never collapse.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.crawlers.html_parser import HTMLParser
from app.discovery.intent.company_site import CompanySiteIntentPlugin
from app.discovery.sources.status import SourceStatus
from app.engines.lead.lead_models import IntentEvidenceType
from tests.fixtures.intent_pages import (
    EXPANSION_ABOUT_HTML,
    HIRING_CAREERS_HTML,
    NO_INTENT_HTML,
    PROJECT_HOME_HTML,
)

BASE = "https://texasskylineco.com"


class _FakeFetcher:
    """Serves saved HTML by URL path; unknown paths are unreachable."""

    def __init__(self, pages):
        self.pages = pages  # path -> html
        self.fetched: list[str] = []

    def fetch(self, url):
        self.fetched.append(url)
        html = self.pages.get(urlsplit(url).path or "/")
        if html is None:
            return None
        return HTMLParser().parse(html, base_url=url)


def _plugin(pages):
    return CompanySiteIntentPlugin(page_fetcher=_FakeFetcher(pages))


class TestProjectSignal:

    def test_project_signal_is_traceable_evidence(self):
        plugin = _plugin({"/": PROJECT_HOME_HTML})
        status, evidence, _ = plugin.collect_evidence(
            company_name="Texas Skyline Roofing",
            website=BASE,
        )
        assert status is SourceStatus.SUCCESS
        assert len(evidence) == 1
        ev = evidence[0]
        assert ev.type is IntentEvidenceType.project
        assert ev.source_url == f"{BASE}/"
        assert ev.source == "company_site"
        assert "currently working on" in ev.snippet

    def test_bare_domain_gets_https_scheme(self):
        plugin = _plugin({"/": PROJECT_HOME_HTML})
        status, evidence, _ = plugin.collect_evidence(
            company_name="Texas Skyline Roofing",
            website="texasskylineco.com",
        )
        assert status is SourceStatus.SUCCESS
        assert evidence[0].source_url == f"{BASE}/"


class TestMultipleSignals:

    def test_project_and_hiring_on_different_pages(self):
        plugin = _plugin({"/": PROJECT_HOME_HTML, "/careers": HIRING_CAREERS_HTML})
        status, evidence, metadata = plugin.collect_evidence(
            company_name="Texas Skyline Roofing",
            website=BASE,
        )
        assert status is SourceStatus.SUCCESS
        kinds = {e.type for e in evidence}
        assert kinds == {IntentEvidenceType.project, IntentEvidenceType.hiring}
        urls = {e.source_url for e in evidence}
        assert urls == {f"{BASE}/", f"{BASE}/careers"}
        assert metadata["pages_fetched"] >= 1

    def test_expansion_signal(self):
        plugin = _plugin({"/about": EXPANSION_ABOUT_HTML})
        status, evidence, _ = plugin.collect_evidence(
            company_name="Texas Skyline Roofing",
            website=BASE,
        )
        assert status is SourceStatus.SUCCESS
        assert [e.type for e in evidence] == [IntentEvidenceType.expansion]
        assert evidence[0].source_url == f"{BASE}/about"


class TestHonestOutcomes:

    def test_no_signal_is_empty_not_unavailable(self):
        plugin = _plugin({"/": NO_INTENT_HTML})
        status, evidence, _ = plugin.collect_evidence(
            company_name="Acme Roofing",
            website=BASE,
        )
        assert status is SourceStatus.EMPTY
        assert evidence == []

    def test_all_pages_unreachable_is_unavailable(self):
        plugin = _plugin({})
        status, evidence, metadata = plugin.collect_evidence(
            company_name="Acme Roofing",
            website=BASE,
        )
        assert status is SourceStatus.UNAVAILABLE
        assert evidence == []
        assert metadata["note"] == "no candidate page reachable"

    def test_no_website_is_empty_and_makes_no_fetches(self):
        fetcher = _FakeFetcher({})
        plugin = CompanySiteIntentPlugin(page_fetcher=fetcher)
        status, evidence, metadata = plugin.collect_evidence(
            company_name="Acme Roofing",
            website="",
        )
        assert status is SourceStatus.EMPTY
        assert evidence == []
        assert metadata["note"] == "no website to crawl"
        assert fetcher.fetched == []
