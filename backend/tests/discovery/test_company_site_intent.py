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
    CHROME_ONLY_SITE,
    CHROME_SITE,
    EXPANSION_ABOUT_HTML,
    HIRING_CAREERS_HTML,
    NO_INTENT_HTML,
    PROJECT_HOME_HTML,
    chrome_page,
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


class TestSiteChrome:
    """A phrase on most of a site's pages is the template, not the page.

    Regression guard for the live defect of 2026-09-18: the footer careers
    promo on turnerconstruction.com turned ``/``, ``/services``,
    ``/projects`` and ``/insights`` into four "hiring" evidence rows while
    none of those pages said anything about hiring. The rule is phrase
    frequency across the pages already fetched — no model, no AI.
    """

    def test_the_shared_footer_is_not_evidence(self):
        plugin = _plugin(CHROME_SITE)
        status, evidence, metadata = plugin.collect_evidence(
            company_name="Texas Skyline Roofing",
            website=BASE,
        )

        urls = {e.source_url for e in evidence}
        assert f"{BASE}/" not in urls
        assert f"{BASE}/services" not in urls
        assert f"{BASE}/projects" not in urls, (
            "a page whose only hiring phrase is the global footer carries "
            "no hiring evidence"
        )
        assert metadata["chrome_matches"] > 0

    def test_the_genuine_hiring_page_is_kept(self):
        """The other half of the rule: real page content must survive.

        ``/careers`` matches the footer too, but it also says "Now hiring"
        in its own meta description. Dropping the whole page would lose the
        one true signal on the site.
        """
        plugin = _plugin(CHROME_SITE)
        status, evidence, _ = plugin.collect_evidence(
            company_name="Texas Skyline Roofing",
            website=BASE,
        )

        assert status is SourceStatus.SUCCESS
        hiring = [e for e in evidence if e.type is IntentEvidenceType.hiring]
        assert len(hiring) == 1
        assert hiring[0].source_url == f"{BASE}/careers"
        assert "now hiring" in hiring[0].snippet.lower(), (
            "the cited phrase must be the page's own words, not the footer's"
        )

    def test_a_site_that_is_all_chrome_is_empty_and_says_why(self):
        """Suppression must never be silent (CLAUDE.md §6)."""
        plugin = _plugin(CHROME_ONLY_SITE)
        status, evidence, metadata = plugin.collect_evidence(
            company_name="Texas Skyline Roofing",
            website=BASE,
        )

        assert status is SourceStatus.EMPTY
        assert evidence == []
        assert metadata["note"] == "only site-wide chrome matched"
        assert metadata["chrome_phrases"] == ["join our team"]

    def test_a_sample_too_small_to_call_anything_chrome_is_left_alone(self):
        """Two pages sharing a sentence is not a site-wide pattern.

        Without this floor a site whose only two reachable pages both
        mention hiring would have BOTH signals suppressed — the rule would
        destroy the evidence it exists to protect.
        """
        site = {
            "/": chrome_page("Acme Roofing", "  <p>Roofing in Dallas.</p>"),
            "/about": chrome_page("About | Acme Roofing", "  <p>Since 1998.</p>"),
        }
        plugin = _plugin(site)
        status, evidence, _ = plugin.collect_evidence(
            company_name="Acme Roofing",
            website=BASE,
        )

        assert status is SourceStatus.SUCCESS
        assert {e.type for e in evidence} == {IntentEvidenceType.hiring}
        assert len(evidence) == 2
