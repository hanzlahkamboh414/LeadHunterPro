"""API-free directory crawl source — real company discovery.

:class:`DirectoryCrawlSource` is the first discovery source that produces
companies without any search API key. It connects the existing
:class:`~app.engines.source_intelligence.source_planner.SourcePlanner` to
the existing crawler / extractor / classifier — reusing production code
wholesale (CLAUDE.md §14), per Blueprint §4:

    SourcePlanner → HTTPCrawler → HTMLParser → CompanyExtractor
                                                  → ContractorClassifier

This is a REAL discovery path (CLAUDE.md §1, §2, §10): every company
record originates from a live page fetch and extraction. When the crawl
cannot reach any seed host the source reports ``UNAVAILABLE`` — "I could
not look" — which is distinct from ``EMPTY`` ("I looked and found
nothing") and must never be collapsed into it (CLAUDE.md §12).

Why this is a new file (Blueprint §8.3):
    Nothing in the codebase connected ``SourcePlanner`` output to
    ``HTTPCrawler`` input. This source is the single bridge component;
    it adds no second abstraction. ``SearchProviderSource`` (priority 50)
    remains for when a search provider is configured, and
    ``FixtureSource`` (priority 999) stays the last-resort emergency
    bridge — never the primary path (§1).

Why ``_parse_location`` is a function-local import:
    ``app.connectors.texas_procurement`` imports ``app.discovery``
    (registration, orchestrator, sources), so a module-level
    ``from app.connectors...`` here would create a discovery↔connectors
    import cycle. The deferred import runs only after every module is
    loaded (Blueprint §9.2, Finding 1; mirrors
    ``fixture_source``:99 / ``search_provider_source``:80).

Why crawler imports are function-local:
    ``app/crawlers/__init__`` eagerly imports ``http_crawler`` and
    ``session_manager``, so a top-level import would pull aiohttp into
    every module that merely imports this source — the same boundary
    :class:`~app.discovery.website.page_fetcher.CrawlerPageFetcher`
    draws.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from app.discovery.sources.base_source import BaseSource
from app.discovery.sources.status import SourceStatus
from app.engines.source_intelligence.source_models import SourcePlannerRequest
from app.engines.source_intelligence.source_planner import SourcePlanner
from app.search_providers.company_extractor import CompanyExtractor
from app.search_providers.contractor_classifier import ContractorClassifier

if TYPE_CHECKING:
    from app.crawlers.config import CrawlerConfig
    from app.crawlers.html_parser import ParsedPage
    from app.crawlers.response import CrawlResponse
    from app.engines.source_intelligence.source_models import SourceRecord
    from app.search_providers.company_extractor import CompanyProfile

logger = logging.getLogger(__name__)

#: Per-page timeout in seconds. Kept shorter than the crawler default so a
#: slow directory cannot stall the whole pipeline (Blueprint §4).
DEFAULT_TIMEOUT = 10


class DirectoryCrawlSource(BaseSource):
    """Crawls planned public directory sites to discover construction firms.

    The pipeline is fully API-free: seed URLs come from SourcePlanner,
    pages are fetched with the production HTTPCrawler (robots.txt, rate
    limiting, retry and caching all inherited), and companies are produced
    by CompanyExtractor + ContractorClassifier. No search-provider key is
    required for this source to run (Blueprint §4 guarantee).
    """

    source_name = "directory_crawl"
    description = "API-free crawl of planned construction directory sites"
    priority = 20  # Above search providers (50) and fixture bridge (999)
    enabled = True

    # Bounded-crawl guardrails (Blueprint §4): the crawl is a bounded
    # fan-out, never a site-wide crawl. A single discover() call is capped
    # in three ways — seeds planned, links followed per seed, and total
    # pages fetched — so a misbehaving directory cannot explode the run.
    MAX_SEEDS = 4
    MAX_LINKS_PER_SEED = 8
    MAX_PAGES_TOTAL = 24

    #: Path tokens that hint a directory member/profile page.
    _MEMBER_PATH_TOKENS: tuple[str, ...] = (
        "member",
        "members",
        "directory",
        "company",
        "companies",
        "profile",
        "business",
        "businesses",
        "contractor",
        "contractors",
        "firm",
        "firms",
        "listing",
        "listings",
        "vendor",
    )

    def __init__(
        self,
        *,
        planner: SourcePlanner | None = None,
        extractor: CompanyExtractor | None = None,
        classifier: ContractorClassifier | None = None,
        config: CrawlerConfig | None = None,
    ) -> None:
        """Configure the source.

        Args:
            planner: Seed planner. Defaults to a real SourcePlanner.
            extractor: Page → CompanyProfile extractor. Defaults to
                CompanyExtractor.
            classifier: Accept/reject gate. Defaults to ContractorClassifier.
            config: Crawler configuration. Defaults to a modest per-page
                timeout (DEFAULT_TIMEOUT) so a slow directory cannot stall
                discovery.
        """
        self._planner = planner or SourcePlanner()
        self._extractor = extractor or CompanyExtractor()
        self._classifier = classifier or ContractorClassifier()
        if config is None:
            from app.crawlers.config import CrawlerConfig

            config = CrawlerConfig(default_timeout=DEFAULT_TIMEOUT)
        self._config = config

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        """Discover construction companies by crawling planned directories.

        Returns:
            (status, company dicts, metadata). Never raises: an unexpected
            error is returned as ``SourceStatus.ERROR`` so the orchestrator
            can continue with the next source (orchestrator contract).
        """
        try:
            return self._discover(industry=industry, location=location, limit=limit)
        except Exception as exc:
            logger.exception("DirectoryCrawlSource: unexpected error")
            return SourceStatus.ERROR, [], {
                "source": self.source_name,
                "error": f"directory crawl failed: {exc}",
            }

    def _discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        """Execute the plan → crawl → extract → classify pipeline."""
        if limit <= 0:
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "reason": "limit_zero",
            }

        # Function-local to avoid the discovery↔connectors import cycle;
        # see the module docstring and Blueprint §9.2.
        from app.connectors.texas_procurement import _parse_location

        city, state = _parse_location(location)

        planned = self._planner.plan(
            SourcePlannerRequest(
                industry=industry,
                country="USA",
                state=state,
                city=city,
            )
        )

        seeds = self._select_seeds(planned.sources, state=state)
        if not seeds:
            logger.warning(
                "DirectoryCrawlSource: planner returned no crawlable seeds for "
                "industry=%r location=%r (total=%d skipped=%d)",
                industry,
                location,
                planned.total_sources,
                planned.skipped_sources,
            )
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "reason": "no_seed_sources",
                "query_summary": planned.query_summary,
                "total_sources": planned.total_sources,
                "skipped_sources": planned.skipped_sources,
            }

        companies, stats = asyncio.run(
            self._crawl_and_extract(
                seeds,
                industry=industry,
                location=location,
                limit=limit,
            )
        )

        metadata: dict[str, Any] = {
            "source": self.source_name,
            "data_source": "live",
            **stats,
            "industry": industry,
            "location": location,
            "limit": limit,
        }
        if companies:
            status = SourceStatus.SUCCESS
            metadata["status"] = SourceStatus.SUCCESS
        elif stats["pages_fetched"] == 0 and stats["fetch_failures"] > 0:
            status = SourceStatus.UNAVAILABLE
            metadata["status"] = SourceStatus.UNAVAILABLE
            metadata["error"] = "all_seed_hosts_unreachable"
        else:
            status = SourceStatus.EMPTY
            metadata["status"] = SourceStatus.EMPTY

        logger.info(
            "DirectoryCrawlSource: %s — %d companies from %d pages "
            "(seeds=%d attempted=%d failures=%d)",
            status.value,
            len(companies),
            stats["pages_fetched"],
            stats["seeds_planned"],
            stats["seeds_attempted"],
            stats["fetch_failures"],
        )
        return status, companies[:limit], metadata

    def _select_seeds(
        self,
        records: list[SourceRecord],
        *,
        state: str | None,
    ) -> list[SourceRecord]:
        """Choose the bounded seed set from the planner's ranked sources.

        Only sources that support company discovery and carry a URL are
        crawlable. When a state is queried, state-specific sources are
        preferred over national ones — the planner sorts flat by priority,
        so a national directory could otherwise crowd out the state's own
        trade associations. The result is capped at ``MAX_SEEDS``.
        """
        crawlable = [r for r in records if r.supports_company_discovery and r.url]
        if state:
            crawlable.sort(
                key=lambda r: (
                    0 if (r.state or "").upper() == state.upper() else 1,
                    r.priority,
                )
            )
        else:
            crawlable.sort(key=lambda r: r.priority)
        return crawlable[: self.MAX_SEEDS]

    async def _crawl_and_extract(
        self,
        seeds: list[SourceRecord],
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Crawl the seed sites and extract/classify candidate companies.

        Imported here, not at module scope: ``app/crawlers/__init__``
        eagerly imports ``http_crawler`` and ``session_manager``, so a
        top-level import would pull aiohttp into every module that merely
        imports this source (see module docstring).

        Returns:
            (company dicts, per-run statistics).
        """
        from app.crawlers.html_parser import HTMLParser
        from app.crawlers.http_crawler import HTTPCrawler

        stats: dict[str, Any] = {
            "seeds_planned": len(seeds),
            "seeds_attempted": 0,
            "pages_attempted": 0,
            "pages_fetched": 0,
            "fetch_failures": 0,
            "companies_accepted": 0,
            "companies_rejected": 0,
        }
        companies: list[dict[str, Any]] = []
        visited: set[str] = set()
        parser = HTMLParser()

        async with HTTPCrawler(self._config) as crawler:
            for seed in seeds:
                if (
                    len(companies) >= limit
                    or stats["pages_attempted"] >= self.MAX_PAGES_TOTAL
                ):
                    break
                if seed.url in visited:
                    continue
                visited.add(seed.url)
                stats["seeds_attempted"] += 1
                seed_response = await self._fetch(crawler, seed.url, stats)
                if seed_response is None:
                    continue
                seed_parsed = parser.parse(
                    seed_response.text, base_url=seed_response.url
                )
                self._consider(
                    seed_response.url,
                    seed_response.text,
                    seed_parsed,
                    industry=industry,
                    location=location,
                    seed=seed,
                    stats=stats,
                    companies=companies,
                    limit=limit,
                )
                if len(companies) >= limit:
                    break
                for link in self._member_links(seed_parsed, seed.url, visited):
                    if (
                        len(companies) >= limit
                        or stats["pages_attempted"] >= self.MAX_PAGES_TOTAL
                    ):
                        break
                    visited.add(link)
                    response = await self._fetch(crawler, link, stats)
                    if response is None:
                        continue
                    parsed = parser.parse(response.text, base_url=response.url)
                    self._consider(
                        link,
                        response.text,
                        parsed,
                        industry=industry,
                        location=location,
                        seed=seed,
                        stats=stats,
                        companies=companies,
                        limit=limit,
                    )
                    if len(companies) >= limit:
                        break

        stats["companies_accepted"] = len(companies)
        return companies, stats

    async def _fetch(
        self,
        crawler: Any,
        url: str,
        stats: dict[str, Any],
    ) -> CrawlResponse | None:
        """Fetch one page; return ``None`` on any failure.

        The real HTTPCrawler returns a failed ``CrawlResponse`` for most
        errors but raises ``CrawlerRobotsBlocked`` before its try block, so
        both paths are handled. One dead page must not abort discovery of
        the other candidates (CLAUDE.md §12: UNAVAILABLE is reported only
        when *every* fetch fails).
        """
        from app.crawlers.base import CrawlRequest

        stats["pages_attempted"] += 1
        try:
            response = await crawler.crawl(
                CrawlRequest(url=url, timeout=self._config.default_timeout)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("DirectoryCrawlSource: %s unreachable — %s", url, exc)
            stats["fetch_failures"] += 1
            return None
        if not response.successful:
            stats["fetch_failures"] += 1
            return None
        if not response.text:
            # Reachable but empty — not a fetch failure; there is simply
            # no content to extract.
            return None
        stats["pages_fetched"] += 1
        return response

    def _member_links(
        self,
        parsed: ParsedPage,
        seed_url: str,
        visited: set[str],
    ) -> list[str]:
        """Same-site links that look like directory member/profile pages.

        Conservative and bounded: only links whose host matches the seed
        site and whose path hints at a member/profile entry are followed.
        External links are never followed, so the crawl stays inside the
        directory site (Blueprint §4: "bounded crawl").
        """
        seed_host = self._host_key(seed_url)
        if not seed_host:
            return []
        candidates: list[str] = []
        for link in parsed.links:
            if link in visited:
                continue
            host = self._host_key(link)
            if not host or host != seed_host:
                continue
            path = urlparse(link).path.lower()
            if any(token in path for token in self._MEMBER_PATH_TOKENS):
                candidates.append(link)
                if len(candidates) >= self.MAX_LINKS_PER_SEED:
                    break
        return candidates

    @staticmethod
    def _host_key(url: str) -> str:
        """Normalized host without the ``www.`` prefix.

        Used to decide whether a link stays inside the seed site and to
        tag provenance. Never raises for malformed URLs — returns empty.
        """
        try:
            return (urlparse(url).netloc or "").lower().removeprefix("www.")
        except ValueError:
            return ""

    def _consider(
        self,
        url: str,
        html: str,
        parsed: ParsedPage,
        *,
        industry: str,
        location: str,
        seed: SourceRecord,
        stats: dict[str, Any],
        companies: list[dict[str, Any]],
        limit: int,
    ) -> None:
        """Extract a company profile from one page and classify it.

        The extractor builds the :class:`CompanyProfile` (name, location,
        trade, contact). The classifier is the accept/reject gate
        (Blueprint §4, line 157): a record is kept only when the classifier
        accepts it AND the extractor did not reject it — e.g. a directory
        index page that the extractor could not classify as construction is
        dropped.
        """
        profile = self._extractor.extract(
            url,
            html,
            title=parsed.title,
            description=parsed.description,
            context={
                "industry_hint": industry,
                "location_hint": location,
            },
        )
        classification = self._classifier.classify(
            name=profile.name,
            title=parsed.title,
            description=profile.industry_focus or parsed.description,
            url=url,
            industry_hint=industry,
        )
        if not classification["accepted"] or profile.is_rejected or not profile.name:
            stats["companies_rejected"] += 1
            logger.debug(
                "DirectoryCrawlSource: rejected %s (%s)",
                url,
                classification.get("reject_reason")
                or profile.rejected_reason
                or "no name",
            )
            return
        companies.append(self._to_company_record(profile, classification, seed))

    def _to_company_record(
        self,
        profile: CompanyProfile,
        classification: dict[str, Any],
        seed: SourceRecord,
    ) -> dict[str, Any]:
        """Project the profile onto the connector's company schema.

        The connector's step-3 filters and ``_normalize_company`` adapter
        consume ``company_name``/``website``/``city``/``state``/
        ``trade_category``/``industry_focus`` plus provenance, so a
        crawl-sourced record is handled identically to a search-sourced
        one (CLAUDE.md §4: every source is replaceable).
        """
        host = self._host_key(profile.website)
        return {
            "company_name": profile.name,
            "website": profile.website,
            "city": profile.city or "",
            "state": profile.state or "",
            "country": profile.country or "USA",
            "trade_category": classification["trade_category"]
            or profile.trade_category,
            "industry_focus": profile.industry_focus,
            "revenue_tier": "",
            "source_url": profile.source_url or profile.website,
            "data_provenance": f"crawl:{host}" if host else "crawl",
            "discovery_reason": f"Crawled from {seed.name} ({seed.url})",
            "confidence": classification.get("confidence", 0.0) or profile.confidence,
        }

    async def health_check(self) -> dict[str, Any]:
        """Report configuration health without performing I/O.

        This source needs no API key and no external registry — its only
        prerequisite is SourcePlanner, which is in-process — so it is
        healthy whenever enabled.
        """
        return {
            "healthy": self.enabled,
            "source": self.source_name,
            "api_keys_required": False,
            "note": "Crawls public directory sites planned by SourcePlanner",
        }
