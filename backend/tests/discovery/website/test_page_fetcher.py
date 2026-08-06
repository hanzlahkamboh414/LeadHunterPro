"""Tests for the Unit 8 page fetcher.

CrawlerPageFetcher is the one place ``app.discovery.website`` is allowed
to depend on the crawler, so these tests pin the two things that makes it
responsible for: absorbing the async boundary, and turning a failed fetch
into ``None`` rather than an exception that would abort the whole run.

The crawler itself is not retested — it has its own suite. It is replaced
with a stub so nothing here touches the network.
"""

from __future__ import annotations

import asyncio

import pytest

from app.discovery.website.extractors import PageContent
from app.discovery.website.page_fetcher import CrawlerPageFetcher, PageFetcher

HTML = """
<html><head><title>Acme Roofing</title></head>
<body><h1>Acme Roofing Company</h1>
<a href="mailto:info@acme.com">email</a></body></html>
"""


class _StubResponse:
    """Stands in for CrawlResponse."""

    def __init__(self, *, text="", successful=True, status_code=200, error=""):
        self.text = text
        self.successful = successful
        self.status_code = status_code
        self.error = error
        self.url = "https://acme.com/"


class _StubCrawler:
    """Stands in for HTTPCrawler; records that it was closed."""

    instances = []

    def __init__(self, *, response=None, boom=None):
        self._response = response
        self._boom = boom
        self.closed = False
        _StubCrawler.instances.append(self)

    async def crawl(self, request):
        if self._boom is not None:
            raise self._boom
        return self._response

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_instances():
    _StubCrawler.instances = []
    yield
    _StubCrawler.instances = []


def _patch_crawler(monkeypatch, *, response=None, boom=None):
    """Replace HTTPCrawler where the fetcher imports it from."""
    import app.crawlers.http_crawler as module

    monkeypatch.setattr(
        module,
        "HTTPCrawler",
        lambda *a, **kw: _StubCrawler(response=response, boom=boom),
    )


class TestContract:
    def test_satisfies_the_protocol(self):
        assert isinstance(CrawlerPageFetcher(), PageFetcher)

    def test_describe_reports_itself(self):
        described = CrawlerPageFetcher(timeout=7).describe()
        assert described["name"] == "crawler_page_fetcher"
        assert described["timeout"] == "7"


class TestSuccess:
    def test_returns_page_content(self, monkeypatch):
        _patch_crawler(monkeypatch, response=_StubResponse(text=HTML))
        page = CrawlerPageFetcher().fetch("https://acme.com/")
        assert page is not None
        assert isinstance(page, PageContent)

    def test_parsed_page_carries_the_html(self, monkeypatch):
        _patch_crawler(monkeypatch, response=_StubResponse(text=HTML))
        page = CrawlerPageFetcher().fetch("https://acme.com/")
        assert page.title == "Acme Roofing"
        assert "Acme Roofing Company" in page.h1_texts

    def test_crawler_is_always_closed(self, monkeypatch):
        _patch_crawler(monkeypatch, response=_StubResponse(text=HTML))
        CrawlerPageFetcher().fetch("https://acme.com/")
        assert _StubCrawler.instances[0].closed is True


class TestFailuresReturnNone:
    """One dead page must not abort discovery of the others."""

    def test_crawler_exception_returns_none(self, monkeypatch):
        _patch_crawler(monkeypatch, boom=RuntimeError("dns failure"))
        assert CrawlerPageFetcher().fetch("https://dead.com/") is None

    def test_crawler_is_closed_even_on_failure(self, monkeypatch):
        _patch_crawler(monkeypatch, boom=RuntimeError("dns failure"))
        CrawlerPageFetcher().fetch("https://dead.com/")
        assert _StubCrawler.instances[0].closed is True

    def test_non_2xx_returns_none(self, monkeypatch):
        _patch_crawler(
            monkeypatch,
            response=_StubResponse(text="", successful=False, status_code=404),
        )
        assert CrawlerPageFetcher().fetch("https://missing.com/") is None

    def test_empty_body_returns_none(self, monkeypatch):
        _patch_crawler(monkeypatch, response=_StubResponse(text=""))
        assert CrawlerPageFetcher().fetch("https://blank.com/") is None


class TestAsyncBoundary:
    """fetch() is sync; calling it from a loop must fail loudly."""

    def test_raises_inside_a_running_event_loop(self, monkeypatch):
        _patch_crawler(monkeypatch, response=_StubResponse(text=HTML))

        async def _inside_loop():
            return CrawlerPageFetcher().fetch("https://acme.com/")

        with pytest.raises(RuntimeError, match="running event loop"):
            asyncio.run(_inside_loop())
