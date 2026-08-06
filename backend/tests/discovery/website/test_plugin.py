"""Tests for DirectWebsiteDiscoveryPlugin (Unit 8).

The plugin is an orchestrator. These tests verify the order it calls its
collaborators in, the status it reports for each outcome, and the shape
of what it emits — not how any collaborator does its job.

The status assertions carry most of the weight. Getting EMPTY and
UNAVAILABLE the wrong way round is the exact failure CLAUDE.md §12
forbids: it reports "no companies exist" when the truth is "I could not
look".

Generators and the page fetcher are stubbed. The real ExtractorManager is
used, because the point of Unit 8 is that the wiring works end to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

import pytest

from app.discovery.plugins.base_plugin import BaseDiscoveryPlugin, PluginCapability
from app.discovery.plugins.plugin_config import PluginConfig
from app.discovery.sources.status import SourceStatus
from app.discovery.website.candidates import Candidate, CandidateGenerator
from app.discovery.website.plugin import DirectWebsiteDiscoveryPlugin


@dataclass
class _StubPage:
    """A PageContent whose ten members mirror ParsedPage."""

    url: str = "https://acme-roofing.com/"
    title: str = ""
    description: str = ""
    emails: list[str] = dataclass_field(default_factory=list)
    phones: list[str] = dataclass_field(default_factory=list)
    social_links: dict[str, str] = dataclass_field(default_factory=dict)
    text_content: str = ""
    links: list[str] = dataclass_field(default_factory=list)
    h1_texts: list[str] = dataclass_field(default_factory=list)
    meta_keywords: str = ""


def _company_page(url: str = "https://acme-roofing.com/") -> _StubPage:
    """A page rich enough to yield a full company record."""
    return _StubPage(
        url=url,
        title="Acme Roofing | Home",
        description="Acme Roofing provides commercial roofing.",
        emails=["info@acme-roofing.com"],
        phones=["(555) 123-4567"],
        social_links={"linkedin": "https://linkedin.com/company/acme"},
        text_content=(
            "Our founder Jane Doe, President, started the company. "
            "Office: 123 Main St, Dallas, TX 75201. "
            "We provide concrete work."
        ),
        h1_texts=["Acme Roofing Company"],
        meta_keywords="roofing, excavation",
    )


class _StubGenerator(CandidateGenerator):
    """Yields fixed candidates, or raises to simulate a dead origin."""

    def __init__(self, urls, *, name="stub", boom=None):
        self.name = name
        self._urls = urls
        self._boom = boom
        self.calls = 0

    def generate(self, *, industry, location, limit):
        self.calls += 1
        if self._boom is not None:
            raise self._boom
        return [
            Candidate(url=u, generator=self.generator_name)
            for u in self._urls[:limit]
        ]


class _StubFetcher:
    """Returns canned pages by URL; anything unknown fails to fetch."""

    def __init__(self, pages=None, *, boom=None):
        self._pages = pages or {}
        self._boom = boom
        self.fetched = []

    def fetch(self, url):
        self.fetched.append(url)
        if self._boom is not None:
            raise self._boom
        return self._pages.get(url)


def _plugin(generators=None, fetcher=None, **options):
    config = PluginConfig(name="direct_website_discovery", options=options)
    return DirectWebsiteDiscoveryPlugin(
        config, generators=generators, fetcher=fetcher
    )


class TestContract:
    """The plugin satisfies the framework's plugin contract."""

    def test_is_discovery_plugin(self):
        assert issubclass(DirectWebsiteDiscoveryPlugin, BaseDiscoveryPlugin)

    def test_declares_identity(self):
        assert DirectWebsiteDiscoveryPlugin.name == "direct_website_discovery"
        assert DirectWebsiteDiscoveryPlugin.description
        assert DirectWebsiteDiscoveryPlugin.priority == 20

    def test_class_priority_applies_without_config(self):
        # Runs ahead of SearchProviderSource (50) and FixtureSource (999).
        assert DirectWebsiteDiscoveryPlugin().priority == 20

    def test_config_priority_overrides_the_class_default(self):
        # BaseDiscoveryPlugin documents config as authoritative, and
        # PluginConfig.priority defaults to 100 — so an options-only
        # config legitimately resets priority. Pinned so the override
        # stays a decision rather than a surprise.
        config = PluginConfig(name="direct_website_discovery", priority=5)
        assert DirectWebsiteDiscoveryPlugin(config).priority == 5

    def test_declares_company_discovery(self):
        assert _plugin().supports(PluginCapability.COMPANY_DISCOVERY)

    def test_discover_is_keyword_only(self):
        plugin = _plugin()
        with pytest.raises(TypeError):
            plugin.discover("Roofing", "Dallas", 5)

    def test_returns_three_tuple(self):
        status, companies, metadata = _plugin().discover(
            industry="Roofing", location="Dallas", limit=5
        )
        assert isinstance(status, SourceStatus)
        assert isinstance(companies, list)
        assert isinstance(metadata, dict)


class TestNoGenerators:
    """An empty registry is reported, never silently tolerated (§5)."""

    def test_status_is_unavailable_not_empty(self):
        status, companies, _ = _plugin().discover(
            industry="Roofing", location="Dallas", limit=5
        )
        assert status is SourceStatus.UNAVAILABLE
        assert companies == []

    def test_fallback_reason_is_explicit(self):
        _, _, metadata = _plugin().discover(
            industry="Roofing", location="Dallas", limit=5
        )
        assert "no candidate generators registered" in metadata["fallback_reason"]

    def test_logs_the_diagnostic(self, caplog):
        with caplog.at_level("ERROR"):
            _plugin().discover(industry="Roofing", location="Dallas", limit=5)
        assert "NO CANDIDATE GENERATORS REGISTERED" in caplog.text

    def test_never_returns_fixture_data(self):
        _, companies, metadata = _plugin().discover(
            industry="Roofing", location="Dallas", limit=5
        )
        assert companies == []
        assert metadata["data_source"] == "live"


class TestGeneratorFailures:
    """A raising generator means 'could not run', not 'found nothing'."""

    def test_all_generators_failing_is_unavailable(self):
        gen = _StubGenerator([], boom=RuntimeError("dns dead"))
        status, _, metadata = _plugin(generators=[gen]).discover(
            industry="Roofing", location="Dallas", limit=5
        )
        assert status is SourceStatus.UNAVAILABLE
        assert "every candidate generator failed" in metadata["fallback_reason"]

    def test_failure_detail_is_recorded(self):
        gen = _StubGenerator([], boom=RuntimeError("dns dead"))
        _, _, metadata = _plugin(generators=[gen]).discover(
            industry="Roofing", location="Dallas", limit=5
        )
        assert metadata["generator_failures"][0]["error"] == "dns dead"

    def test_one_failure_does_not_stop_the_others(self):
        dead = _StubGenerator([], name="dead", boom=RuntimeError("down"))
        alive = _StubGenerator(["https://acme-roofing.com/"], name="alive")
        fetcher = _StubFetcher({"https://acme-roofing.com/": _company_page()})

        status, companies, metadata = _plugin(
            generators=[dead, alive], fetcher=fetcher
        ).discover(industry="Roofing", location="Dallas", limit=5)

        assert status is SourceStatus.SUCCESS
        assert len(companies) == 1
        assert len(metadata["generator_failures"]) == 1


class TestDeduplication:
    """Candidates are deduplicated across generators (Unit 2 reused)."""

    def test_same_url_from_two_generators_fetched_once(self):
        a = _StubGenerator(["https://acme-roofing.com/"], name="a")
        b = _StubGenerator(["http://acme-roofing.com/"], name="b")
        fetcher = _StubFetcher({"https://acme-roofing.com/": _company_page()})

        _, _, metadata = _plugin(generators=[a, b], fetcher=fetcher).discover(
            industry="Roofing", location="Dallas", limit=5
        )

        assert metadata["candidates_proposed"] == 2
        assert metadata["duplicates_removed"] == 1
        assert len(fetcher.fetched) == 1

    def test_filter_stats_are_reported(self):
        gen = _StubGenerator(["https://acme-roofing.com/"])
        fetcher = _StubFetcher({"https://acme-roofing.com/": _company_page()})
        _, _, metadata = _plugin(generators=[gen], fetcher=fetcher).discover(
            industry="Roofing", location="Dallas", limit=5
        )
        assert metadata["url_filter"]["accepted"] == 1


class TestFetchOutcomes:
    """Fetch failures are distinguished from genuinely empty results."""

    def test_all_fetches_failing_is_unavailable(self):
        gen = _StubGenerator(["https://unreachable.com/"])
        status, _, metadata = _plugin(
            generators=[gen], fetcher=_StubFetcher({})
        ).discover(industry="Roofing", location="Dallas", limit=5)

        assert status is SourceStatus.UNAVAILABLE
        assert "every candidate page failed to fetch" in metadata["fallback_reason"]

    def test_fetched_but_nameless_page_is_empty(self):
        url = "https://blank.com/"
        gen = _StubGenerator([url])
        fetcher = _StubFetcher({url: _StubPage(url=url)})

        status, companies, metadata = _plugin(
            generators=[gen], fetcher=fetcher
        ).discover(industry="Roofing", location="Dallas", limit=5)

        assert status is SourceStatus.EMPTY
        assert companies == []
        assert metadata["companies_rejected"] == 1

    def test_fetcher_raising_is_error_not_empty(self):
        gen = _StubGenerator(["https://acme-roofing.com/"])
        fetcher = _StubFetcher(boom=ValueError("bug"))

        status, _, metadata = _plugin(
            generators=[gen], fetcher=fetcher
        ).discover(industry="Roofing", location="Dallas", limit=5)

        assert status is SourceStatus.ERROR
        assert metadata["error"] == "bug"


class TestSuccess:
    """The happy path produces company records with provenance."""

    def _discover(self):
        url = "https://acme-roofing.com/"
        gen = _StubGenerator([url])
        fetcher = _StubFetcher({url: _company_page(url)})
        return _plugin(generators=[gen], fetcher=fetcher).discover(
            industry="Roofing", location="Dallas", limit=5
        )

    def test_status_is_success(self):
        status, companies, _ = self._discover()
        assert status is SourceStatus.SUCCESS
        assert len(companies) == 1

    def test_company_carries_singular_fields(self):
        _, companies, _ = self._discover()
        company = companies[0]
        assert company["name"]
        assert company["phone"] == "(555) 123-4567"
        assert company["email"] == "info@acme-roofing.com"
        assert company["address"]

    def test_company_carries_plural_fields(self):
        _, companies, _ = self._discover()
        company = companies[0]
        assert "roofing" in company["services"]
        assert company["leadership"][0]["title"]
        assert company["social"]["linkedin"]

    def test_company_is_marked_live_not_bridge(self):
        _, companies, _ = self._discover()
        assert companies[0]["data_source"] == "live"
        assert companies[0]["bridge_mode"] is False

    def test_company_carries_provenance(self):
        _, companies, _ = self._discover()
        company = companies[0]
        assert company["website"] == "https://acme-roofing.com/"
        assert company["discovered_by"] == "stub"
        assert company["evidence"]

    def test_name_prefers_highest_confidence(self):
        # Title is HIGH, h1 MEDIUM, description LOW — title must win.
        _, companies, _ = self._discover()
        assert companies[0]["name"] == "Acme Roofing"


class TestBudgets:
    """Discovery is bounded — crawling is the expensive step."""

    def test_limit_caps_companies(self):
        urls = [f"https://company{i}.com/" for i in range(5)]
        gen = _StubGenerator(urls)
        fetcher = _StubFetcher({u: _company_page(u) for u in urls})

        _, companies, _ = _plugin(generators=[gen], fetcher=fetcher).discover(
            industry="Roofing", location="Dallas", limit=2
        )
        assert len(companies) == 2

    def test_max_pages_caps_fetches(self):
        urls = [f"https://company{i}.com/" for i in range(5)]
        gen = _StubGenerator(urls)
        fetcher = _StubFetcher({})

        _, _, metadata = _plugin(
            generators=[gen], fetcher=fetcher, max_pages=2
        ).discover(industry="Roofing", location="Dallas", limit=10)

        assert metadata["pages_attempted"] == 2
        assert len(fetcher.fetched) == 2


class TestMetadata:
    """Every run reports what happened (§6 logging standard)."""

    def test_reports_the_full_execution_record(self):
        url = "https://acme-roofing.com/"
        gen = _StubGenerator([url])
        fetcher = _StubFetcher({url: _company_page(url)})
        _, _, metadata = _plugin(generators=[gen], fetcher=fetcher).discover(
            industry="Roofing", location="Dallas", limit=5
        )

        for key in (
            "plugin",
            "query",
            "generators_found",
            "candidates_proposed",
            "duplicates_removed",
            "pages_attempted",
            "pages_fetched",
            "fetch_failures",
            "companies_accepted",
            "companies_rejected",
            "data_source",
        ):
            assert key in metadata, f"metadata missing {key!r}"

    def test_query_is_echoed(self):
        _, _, metadata = _plugin().discover(
            industry="Roofing", location="Dallas", limit=5
        )
        assert metadata["query"]["industry"] == "Roofing"


class TestFetcherInjection:
    """Page retrieval is injected, never built into the plugin."""

    def test_injected_fetcher_is_used(self):
        url = "https://acme-roofing.com/"
        fetcher = _StubFetcher({url: _company_page(url)})
        _plugin(generators=[_StubGenerator([url])], fetcher=fetcher).discover(
            industry="Roofing", location="Dallas", limit=5
        )
        assert fetcher.fetched == [url]

    def test_default_fetcher_is_not_built_eagerly(self):
        # Constructing the plugin must not construct a crawler.
        plugin = _plugin()
        assert plugin._fetcher is None

    def test_default_fetcher_is_the_crawler_one(self):
        from app.discovery.website.page_fetcher import CrawlerPageFetcher

        assert isinstance(_plugin().fetcher, CrawlerPageFetcher)


class TestHealthCheck:
    """health_check reports configuration without performing I/O."""

    @pytest.mark.asyncio
    async def test_unhealthy_without_generators(self):
        health = await _plugin().health_check()
        assert health["healthy"] is False
        assert "no candidate generators" in health["reason"]

    @pytest.mark.asyncio
    async def test_reports_generators_and_extractors(self):
        gen = _StubGenerator([], name="alive")
        health = await _plugin(generators=[gen]).health_check()
        assert health["generators"] == ["alive"]
        assert health["extractors"] == 7
