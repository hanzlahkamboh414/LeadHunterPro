"""Tests for DirectoryCrawlSource (Blueprint §6 Phase 3).

The source is the API-free primary path: SourcePlanner → HTTPCrawler →
HTMLParser → CompanyExtractor → ContractorClassifier. These tests prove
the real pipeline end to end while replacing only the network:

- ``HTTPCrawler`` is stubbed (patched at its origin module, exactly like
  ``test_page_fetcher``) so nothing here touches the network;
- SourcePlanner is replaced with a tiny fake so the seed set is
  deterministic (the real planner's seed URLs are real sites that a test
  must not hit);
- CompanyExtractor and ContractorClassifier are the REAL production
  classes — the extract→classify→output contract is exercised, not
  mocked.

Status semantics pinned here (CLAUDE.md §12):
  SUCCESS      — at least one company extracted
  UNAVAILABLE  — every fetch failed (I could not look)
  EMPTY        — pages fetched but nothing matched (I looked, found none)
  ERROR        — the source raised unexpectedly, never to the orchestrator
"""

from __future__ import annotations

import asyncio

import pytest

from app.crawlers.response import CrawlResponse
from app.discovery.sources.base_source import BaseSource
from app.discovery.sources.directory_crawl_source import DirectoryCrawlSource
from app.discovery.sources.status import SourceStatus
from app.engines.source_intelligence.source_models import (
    SourcePlannerResult,
    SourceRecord,
)

# ---------------------------------------------------------------------------
# Stub network layer
# ---------------------------------------------------------------------------

#: Shared mutable state the stub crawler reads on every call. Reset per test.
_STUB_STATE: dict = {
    "pages": {},
    "fail_urls": set(),
    "boom_urls": set(),
    "boom_on_enter": None,
    "instances": [],
}


def _ok_response(url: str, html: str) -> CrawlResponse:
    """A successful CrawlResponse carrying *html*."""
    return CrawlResponse(
        url=url,
        status_code=200,
        content=html.encode("utf-8"),
        headers={},
        successful=True,
        robots_compliant=True,
    )


def _failed_response(url: str, *, error: str = "simulated dns failure") -> CrawlResponse:
    """A failed CrawlResponse — the shape HTTPCrawler returns on network errors."""
    return CrawlResponse(
        url=url,
        status_code=0,
        content=b"",
        headers={},
        successful=False,
        robots_compliant=True,
        error=error,
    )


class _StubCrawler:
    """Stands in for HTTPCrawler; serves fixture HTML with no network.

    ``__aenter__`` can be told to raise to exercise the source's ERROR
    path; ``crawl`` can be told to raise (transport) or return a failed
    response (DNS) to exercise the UNAVAILABLE path.
    """

    def __init__(self, config=None):
        self.config = config
        self.closed = False
        _STUB_STATE["instances"].append(self)

    async def __aenter__(self):
        if _STUB_STATE["boom_on_enter"] is not None:
            raise _STUB_STATE["boom_on_enter"]
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def crawl(self, request):
        url = request.url
        if url in _STUB_STATE["boom_urls"]:
            raise RuntimeError("simulated transport failure")
        if url in _STUB_STATE["fail_urls"]:
            return _failed_response(url)
        html = _STUB_STATE["pages"].get(url)
        if html is None:
            # Unplanned URL: treat as unreachable, as the real crawler
            # would for a dead/missing page.
            return _failed_response(url, error="no fixture for url")
        return _ok_response(url, html)


@pytest.fixture(autouse=True)
def _patch_crawler(monkeypatch):
    """Replace HTTPCrawler at its origin module and reset stub state."""
    import app.crawlers.http_crawler as module

    _STUB_STATE["pages"] = {}
    _STUB_STATE["fail_urls"] = set()
    _STUB_STATE["boom_urls"] = set()
    _STUB_STATE["boom_on_enter"] = None
    _STUB_STATE["instances"] = []
    monkeypatch.setattr(module, "HTTPCrawler", _StubCrawler)
    yield
    _STUB_STATE["instances"] = []


# ---------------------------------------------------------------------------
# Fixture HTML
# ---------------------------------------------------------------------------

_SEED_URL = "https://www.texascontractor.com/directory"


def _directory_html(member_urls: list[str]) -> str:
    """A directory index page linking to member profiles."""
    links = "".join(
        f'<a href="{u}">{i}: member</a>' for i, u in enumerate(member_urls)
    )
    return (
        "<html><head><title>Texas Contractor Directory</title>"
        '<meta name="description" content="Directory of Texas contractors.">'
        f"</head><body><h1>Member Directory</h1>{links}</body></html>"
    )


ACME_HTML = """
<html><head>
<title>Acme Roofing LLC - Roofing Contractor in Dallas</title>
<meta property="og:title" content="Acme Roofing LLC">
<meta name="description"
 content="Commercial roofing, shingle replacement and TPO installation by Acme Roofing LLC serving Dallas, TX.">
<script type="application/ld+json">
{"@type":"Organization","name":"Acme Roofing LLC",
 "address":{"addressLocality":"Dallas","addressRegion":"TX"}}
</script>
</head>
<body><h1>Acme Roofing LLC</h1>
<p>Acme Roofing LLC provides commercial roofing, shingle replacement and
TPO installation in Dallas, Texas.</p>
</body></html>
"""

JONES_HTML = """
<html><head>
<title>Jones Plumbing Company</title>
<meta property="og:title" content="Jones Plumbing Company">
<meta name="description"
 content="Plumbing services, pipe repair and drain cleaning in Houston, TX.">
</head>
<body><h1>Jones Plumbing Company</h1>
<p>Jones Plumbing Company provides plumbing services, pipe repair and
drain cleaning in Houston, Texas.</p>
</body></html>
"""

NEWS_HTML = """
<html><head><title>Construction News Weekly</title>
<meta name="description" content="Latest industry news and analysis.">
</head>
<body><h1>Weekly</h1></body></html>
"""

_MEMBER_URLS = {
    "acme": "https://www.texascontractor.com/member/acme-roofing",
    "jones": "https://www.texascontractor.com/member/jones-plumbing",
}


# ---------------------------------------------------------------------------
# Fake planner
# ---------------------------------------------------------------------------


class _FakePlanner:
    """Returns a fixed, deterministic seed set regardless of the query."""

    def __init__(self, *records: SourceRecord) -> None:
        self.records = list(records)

    def plan(self, request):
        return SourcePlannerResult(
            sources=self.records,
            query_summary="test",
            total_sources=len(self.records),
            skipped_sources=0,
        )


def _seed(
    name: str = "Texas Contractor Bulletin Directory",
    url: str = _SEED_URL,
) -> SourceRecord:
    return SourceRecord(
        name=name,
        source_type="industry_publication",
        country="USA",
        state="TX",
        priority=5,
        url=url,
        supports_company_discovery=True,
    )


def _source_with(*records: SourceRecord) -> DirectoryCrawlSource:
    return DirectoryCrawlSource(planner=_FakePlanner(*records))


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class TestContract:
    def test_is_a_base_source(self):
        source = DirectoryCrawlSource()
        assert isinstance(source, BaseSource)

    def test_priority_is_above_search_providers(self):
        assert DirectoryCrawlSource().priority == 20

    def test_enabled_by_default(self):
        assert DirectoryCrawlSource().enabled is True

    def test_health_check_reports_no_keys_required(self):
        report = asyncio.run(DirectoryCrawlSource().health_check())
        assert report["healthy"] is True
        assert report["api_keys_required"] is False


# ---------------------------------------------------------------------------
# Happy path: real extract → classify → output
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_extracts_classifies_and_returns_companies(self):
        acme, jones = _MEMBER_URLS["acme"], _MEMBER_URLS["jones"]
        _STUB_STATE["pages"] = {
            _SEED_URL: _directory_html([acme, jones]),
            acme: ACME_HTML,
            jones: JONES_HTML,
        }
        source = _source_with(_seed())

        status, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        assert status == SourceStatus.SUCCESS
        assert meta["data_source"] == "live"
        assert len(companies) == 2
        assert [c["company_name"] for c in companies] == [
            "Acme Roofing LLC",
            "Jones Plumbing Company",
        ]

    def test_records_carry_connector_schema(self):
        acme = _MEMBER_URLS["acme"]
        _STUB_STATE["pages"] = {
            _SEED_URL: _directory_html([acme]),
            acme: ACME_HTML,
        }
        source = _source_with(_seed())

        status, companies, _ = source.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        assert status == SourceStatus.SUCCESS
        company = companies[0]
        assert company["company_name"] == "Acme Roofing LLC"
        assert company["website"] == acme
        assert company["city"] == "Dallas"
        assert company["state"] == "TX"
        assert company["country"] == "USA"
        assert company["trade_category"] == "roofing"
        assert company["source_url"] == acme
        assert company["data_provenance"].startswith("crawl:")
        assert "Crawled from" in company["discovery_reason"]

    def test_directory_index_page_is_not_emitted(self):
        acme = _MEMBER_URLS["acme"]
        _STUB_STATE["pages"] = {
            _SEED_URL: _directory_html([acme]),
            acme: ACME_HTML,
        }
        source = _source_with(_seed())

        _, companies, _ = source.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        # The seed page itself (a directory index) is rejected; only the
        # member pages it links to become companies.
        assert all(c["company_name"] != "Texas Contractor Directory" for c in companies)

    def test_external_links_are_not_followed(self):
        acme = _MEMBER_URLS["acme"]
        external = "https://www.example.com/member/outsider"
        _STUB_STATE["pages"] = {
            _SEED_URL: _directory_html([acme, external]),
            acme: ACME_HTML,
            external: JONES_HTML,
        }
        source = _source_with(_seed())

        _, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        # The external link stays outside the crawl: only the same-site
        # member link is followed (Blueprint §4: bounded crawl).
        assert len(companies) == 1
        assert companies[0]["website"] == acme
        assert meta["pages_attempted"] == 2  # seed + one same-site member

    def test_limit_is_respected(self):
        acme, jones = _MEMBER_URLS["acme"], _MEMBER_URLS["jones"]
        _STUB_STATE["pages"] = {
            _SEED_URL: _directory_html([acme, jones]),
            acme: ACME_HTML,
            jones: JONES_HTML,
        }
        source = _source_with(_seed())

        _, companies, _ = source.discover(
            industry="Roofing", location="Dallas Texas", limit=1
        )

        assert len(companies) == 1


# ---------------------------------------------------------------------------
# Bounded-crawl guardrails
# ---------------------------------------------------------------------------


class TestBoundedCrawl:
    def test_member_links_capped_per_seed(self):
        member_urls = [
            f"https://www.texascontractor.com/member/firm-{i}" for i in range(30)
        ]
        _STUB_STATE["pages"] = {_SEED_URL: _directory_html(member_urls)}
        _STUB_STATE["pages"].update({u: ACME_HTML for u in member_urls})
        source = _source_with(_seed())

        _, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=100
        )

        # Seed page + at most MAX_LINKS_PER_SEED member pages.
        assert meta["pages_attempted"] == 1 + source.MAX_LINKS_PER_SEED
        assert len(companies) == source.MAX_LINKS_PER_SEED

    def test_seeds_capped_by_max_seeds(self):
        many_seeds = [
            _seed(name=f"Seed {i}", url=f"https://www.texascontractor.com/dir{i}")
            for i in range(10)
        ]
        for seed in many_seeds:
            _STUB_STATE["pages"][seed.url] = _directory_html([])
        source = _source_with(*many_seeds)

        _, _, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        assert meta["seeds_attempted"] <= source.MAX_SEEDS


# ---------------------------------------------------------------------------
# Status semantics
# ---------------------------------------------------------------------------


class TestStatus:
    def test_unavailable_when_all_seed_hosts_unreachable(self):
        _STUB_STATE["fail_urls"] = {_SEED_URL}
        source = _source_with(_seed())

        status, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        assert status == SourceStatus.UNAVAILABLE
        assert companies == []
        assert meta["error"] == "all_seed_hosts_unreachable"

    def test_unavailable_when_crawl_raises(self):
        _STUB_STATE["boom_urls"] = {_SEED_URL}
        source = _source_with(_seed())

        status, _, _ = source.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        assert status == SourceStatus.UNAVAILABLE

    def test_empty_when_pages_fetched_but_nothing_matches(self):
        _STUB_STATE["pages"] = {_SEED_URL: NEWS_HTML}
        source = _source_with(_seed())

        status, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        assert status == SourceStatus.EMPTY
        assert companies == []
        assert meta["pages_fetched"] >= 1

    def test_empty_when_planner_returns_no_crawlable_seeds(self):
        source = _source_with()

        status, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        assert status == SourceStatus.EMPTY
        assert companies == []
        assert meta["reason"] == "no_seed_sources"

    def test_error_when_crawler_setup_fails(self):
        _STUB_STATE["boom_on_enter"] = RuntimeError("boom")
        source = _source_with(_seed())

        status, companies, meta = source.discover(
            industry="Roofing", location="Dallas Texas", limit=10
        )

        assert status == SourceStatus.ERROR
        assert companies == []
        assert "boom" in meta["error"]


# ---------------------------------------------------------------------------
# Seed selection
# ---------------------------------------------------------------------------


class TestSeedSelection:
    def test_drops_sources_without_url_or_company_discovery(self):
        source = DirectoryCrawlSource()
        records = [
            SourceRecord(
                name="No URL",
                source_type="trade_association",
                country="USA",
                supports_company_discovery=True,
            ),
            SourceRecord(
                name="No discovery support",
                source_type="government",
                country="USA",
                url="https://www.gov.example/",
                supports_company_discovery=False,
            ),
            SourceRecord(
                name="Good",
                source_type="trade_association",
                country="USA",
                url="https://www.good.example/",
                supports_company_discovery=True,
            ),
        ]

        selected = source._select_seeds(records, state="TX")

        assert [r.name for r in selected] == ["Good"]

    def test_prefers_state_specific_sources_when_state_queried(self):
        source = DirectoryCrawlSource()
        records = [
            SourceRecord(
                name="National A",
                source_type="business_directory",
                country="USA",
                priority=1,
                url="https://www.national.com/",
                supports_company_discovery=True,
            ),
            SourceRecord(
                name="TX Assoc",
                source_type="trade_association",
                country="USA",
                state="TX",
                priority=5,
                url="https://www.txassoc.com/",
                supports_company_discovery=True,
            ),
        ]

        selected = source._select_seeds(records, state="TX")

        assert [r.name for r in selected] == ["TX Assoc", "National A"]

    def test_flat_priority_order_when_no_state(self):
        source = DirectoryCrawlSource()
        records = [
            SourceRecord(
                name="Prio 5",
                source_type="business_directory",
                country="USA",
                priority=5,
                url="https://www.five.com/",
                supports_company_discovery=True,
            ),
            SourceRecord(
                name="Prio 1",
                source_type="business_directory",
                country="USA",
                priority=1,
                url="https://www.one.com/",
                supports_company_discovery=True,
            ),
        ]

        selected = source._select_seeds(records, state=None)

        assert [r.name for r in selected] == ["Prio 1", "Prio 5"]
