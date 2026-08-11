"""Phase 4 — no search API keys → live crawl results, not fixtures.

Proves the production connector path returns LIVE crawl data when no
search provider is configured (Blueprint §6 Phase 4, §7):

    TexasProcurementConnector.search()
        → SourceOrchestrator
            → DirectoryCrawlSource  (SUCCESS — real planner, stubbed network)
            → SearchProviderSource  (EMPTY   — registry cleared, no keys)
            → FixtureSource         (disabled in live mode — accuracy-first)

Everything is the real production pipeline except the network: HTTPCrawler
is replaced at its origin module so no DNS/TCP occurs (the sandbox blocks
seed-host DNS — Blueprint §8). The planner, extractor, classifier,
orchestrator, connector Step-3 filters and ranking are all REAL.

Status semantics pinned here (CLAUDE.md §12): ``directory_crawl: SUCCESS``
means the API-free source produced companies; ``data_source == "live"``
proves the fixture bridge did NOT become the primary path (§1, §5).
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from app.connectors.texas_procurement import TexasProcurementConnector
from app.crawlers.response import CrawlResponse
from app.discovery.source_orchestrator import SourceOrchestrator
from app.discovery.sources.directory_crawl_source import DirectoryCrawlSource
from app.discovery.sources.fixture_source import FixtureSource
from app.discovery.sources.search_provider_source import SearchProviderSource
from app.discovery.sources.status import SourceStatus
from app.search_providers import clear_registry

# ---------------------------------------------------------------------------
# Fixture HTML
# ---------------------------------------------------------------------------

MEMBER_PROFILE_HTML = """
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

#: ``__MEMBER_URL__`` is replaced with a same-host member link by the stub.
DIRECTORY_INDEX_HTML = """
<html><head><title>Texas Contractor Directory</title>
<meta name="description" content="Directory of Texas contractors.">
</head><body><h1>Member Directory</h1>
<a href="__MEMBER_URL__">Acme Roofing LLC</a></body></html>
"""


# ---------------------------------------------------------------------------
# Stub network layer
# ---------------------------------------------------------------------------


class _NoKeysStubCrawler:
    """Serves a directory index + member profile with no network.

    Any URL whose path looks like a member page returns the contractor
    profile HTML; anything else is treated as a directory index that links
    to a same-host member page. The real SourcePlanner decides the seed
    URLs — the stub answers whatever it is asked for.
    """

    def __init__(self, config=None):
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def crawl(self, request):
        url = request.url
        if "/member/" in urlparse(url).path.lower():
            html = MEMBER_PROFILE_HTML
        else:
            member_url = f"{url.rstrip('/')}/member/acme-roofing"
            html = DIRECTORY_INDEX_HTML.replace("__MEMBER_URL__", member_url)
        return CrawlResponse(
            url=url,
            status_code=200,
            content=html.encode("utf-8"),
            headers={},
            successful=True,
            robots_compliant=True,
        )


@pytest.fixture(autouse=True)
def _no_network_and_no_keys(monkeypatch):
    """Stub the network and guarantee empty provider/plugin registries.

    Three independent gates are neutralized so the crawl source is the
    only path that can succeed:

    - HTTPCrawler → stub (the sandbox blocks seed-host DNS, §8);
    - search-provider registry → empty, so SearchProviderSource is EMPTY;
    - website-discovery plugin registry + live-origin config → empty, so
      register_website_discovery() is a logged no-op (§5).
    """
    import app.crawlers.http_crawler as module

    monkeypatch.setattr(module, "HTTPCrawler", _NoKeysStubCrawler)

    clear_registry()

    # Deferred import: app.discovery.plugins depends on app.discovery.sources,
    # so a top-level import here would import this test's own package first.
    from app.discovery.plugins.plugin_registry import PluginRegistry

    PluginRegistry.reset_instance()
    from app.core.config import settings

    monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", "")
    monkeypatch.setattr(settings, "SEARXNG_URL", "")
    yield
    PluginRegistry.reset_instance()


# ---------------------------------------------------------------------------
# Phase 4 proof
# ---------------------------------------------------------------------------


class TestNoKeysLiveCrawl:
    def test_no_api_keys_returns_live_crawl_results(self):
        connector = TexasProcurementConnector()
        results, metadata = connector.search("Roofing", "Dallas Texas", 10)

        # The whole run is labeled live — never a fixture fallback (§1).
        assert metadata["data_source"] == "live"
        assert metadata["source_metadata"]["bridge_mode"] is False

        stats = metadata["source_metadata"]["source_stats"]
        assert stats["directory_crawl"]["status"] == SourceStatus.SUCCESS
        assert stats["directory_crawl"]["results"] >= 1
        assert stats["search_providers"]["status"] == SourceStatus.EMPTY

        # The crawled member page survived extraction → filter → rank.
        assert any(r.company_name == "Acme Roofing LLC" for r in results)

    def test_crawl_source_actually_fetched_pages(self):
        connector = TexasProcurementConnector()
        _, metadata = connector.search("Roofing", "Dallas Texas", 10)

        crawl_meta = metadata["source_metadata"]["source_stats"][
            "directory_crawl"
        ]["metadata"]
        assert crawl_meta["data_source"] == "live"
        assert crawl_meta["pages_fetched"] >= 2

    def test_health_report_shows_crawl_success_search_empty(self, capsys):
        connector = TexasProcurementConnector()
        _, metadata = connector.search("Roofing", "Dallas Texas", 10)

        # Reconstruct the orchestrator health report from the connector's
        # aggregated source metadata (Blueprint §6 Phase 4 validation).
        orchestrator = SourceOrchestrator()
        orchestrator.register(DirectoryCrawlSource())
        orchestrator.register(SearchProviderSource())
        orchestrator.register(FixtureSource())
        orchestrator.print_health_report([], metadata["source_metadata"])

        report = capsys.readouterr().out
        assert "directory_crawl" in report and "SUCCESS" in report
        assert "search_providers" in report and "EMPTY" in report
