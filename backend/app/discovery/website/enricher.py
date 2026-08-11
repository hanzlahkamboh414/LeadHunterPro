"""Website evidence enricher — fills REAL location evidence from live pages.

The connector's Step-3 filter requires a candidate to carry ``state``/
``city`` for a location query, but search and website-plugin sources
deliberately emit NO location (accuracy-first, Phase 2 Step 3: the query
is search intent, never company location evidence). This stage is the
missing "Visit Websites → Extract Company Information" link (CLAUDE.md
§10): for each candidate that lacks a location, it crawls the candidate's
website and fills ``city``/``state``/``address``/``trade_category`` from
the page's OWN content via the production :class:`CompanyExtractor`.

Nothing is ever fabricated:

- an unreachable site, or a page with no address, stays un-enriched and is
  honestly dropped by the Step-3 filter downstream (absence of evidence is
  not evidence of presence — CLAUDE.md §12);
- a manufacturer / aggregator / login page is rejected by the production
  :class:`ContractorClassifier` gate (the Inc8 fixes apply unchanged);
- ``location_hint`` is deliberately NOT passed to the extractor: the query
  never becomes location evidence (Phase 2 Step 3).

Why the filled ``address`` matters: :class:`LocationVerifier` treats a raw
``city``/``state`` on the record as an unverified *claim* — it only VERIFIES
a location from explicit evidence text. The clean ``"City, ST"`` address
(profile.address, or reconstructed from profile.city/state) is that
evidence, so the verified location survives ``_build_result`` instead of
being overridden to empty. This is the exact contract the connector's gate
already consumes (``company["address"]`` → ``evidence_texts``).

Why reuse, not a new engine (CLAUDE.md §14): the crawl→parse→extract→
classify per-page pipeline already exists in :class:`DirectoryCrawlSource`
and the website plugin. This module is the same pipeline applied to
*already-discovered candidate records* — the only missing edge in the
orchestrator→filter path. HTTPCrawler (robots/rate-limit/retry/cache),
HTMLParser, CompanyExtractor and ContractorClassifier are all reused
wholesale; no crawler or extraction logic is duplicated.

Why crawler imports are function-local: ``app/crawlers/__init__`` eagerly
imports ``http_crawler``, so a top-level import would pull aiohttp into
every module that merely imports the enricher (the same boundary
:class:`~app.discovery.website.page_fetcher.CrawlerPageFetcher` draws).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from app.search_providers.company_extractor import CompanyExtractor
from app.search_providers.contractor_classifier import ContractorClassifier

if TYPE_CHECKING:
    from app.crawlers.config import CrawlerConfig
    from app.crawlers.html_parser import ParsedPage
    from app.crawlers.response import CrawlResponse
    from app.search_providers.company_extractor import CompanyProfile

logger = logging.getLogger(__name__)

#: Per-page timeout (s). Matches DirectoryCrawlSource: a slow site must not
#: stall the whole discovery run.
DEFAULT_TIMEOUT = 10

#: Bounded-crawl cap (Blueprint §4): enrichment is a bounded fan-out over
#: candidate websites, never an unbounded crawl. Kept identical to
#: DirectoryCrawlSource's total-page ceiling so a run cannot explode.
MAX_PAGES = 24


class WebsiteEnricher:
    """Crawl location-less candidates' websites and fill real evidence.

    Input is the orchestrator's aggregate candidate list (heterogeneous
    source dicts). Output is the same list, with every candidate that
    survived the classifier gate kept — hard-rejected candidates (non-
    contractor pages) are dropped, and nothing is ever invented.
    """

    source_name = "website_enrich"

    def __init__(
        self,
        *,
        extractor: CompanyExtractor | None = None,
        classifier: ContractorClassifier | None = None,
        config: CrawlerConfig | None = None,
    ) -> None:
        """Configure the enricher.

        Args:
            extractor: Page → CompanyProfile extractor. Defaults to
                CompanyExtractor (production).
            classifier: Accept/reject gate. Defaults to ContractorClassifier
                (production).
            config: Crawler configuration. Defaults to a modest per-page
                timeout (DEFAULT_TIMEOUT).
        """
        self._extractor = extractor or CompanyExtractor()
        self._classifier = classifier or ContractorClassifier()
        if config is None:
            from app.crawlers.config import CrawlerConfig

            config = CrawlerConfig(default_timeout=DEFAULT_TIMEOUT)
        self._config = config

    def enrich(
        self,
        candidates: list[dict[str, Any]],
        *,
        industry: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Enrich candidates lacking location evidence from their websites.

        Returns:
            (kept candidate dicts, per-run stats). Never raises: an
            unexpected error returns the candidates unchanged with an
            ``error`` stat so discovery continues (orchestrator contract).
        """
        try:
            return self._enrich(candidates, industry=industry, limit=limit)
        except Exception as exc:
            logger.exception("WebsiteEnricher: unexpected error")
            return list(candidates), {
                "source": self.source_name,
                "error": f"website enrichment failed: {exc}",
                "candidates_in": len(candidates),
                "candidates_out": len(candidates),
                "crawled": 0,
                "enriched": 0,
                "rejected": 0,
            }

    def _enrich(
        self,
        candidates: list[dict[str, Any]],
        *,
        industry: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Execute the bounded crawl → extract → classify → fill pipeline."""
        if not candidates or limit <= 0:
            return list(candidates), {
                "source": self.source_name,
                "candidates_in": len(candidates),
                "candidates_out": len(candidates),
                "reason": "no_candidates",
            }

        # Function-local to keep aiohttp out of the import graph (see
        # module docstring; mirrors directory_crawl_source).
        from app.crawlers.html_parser import HTMLParser
        from app.crawlers.http_crawler import HTTPCrawler

        stats: dict[str, Any] = {
            "source": self.source_name,
            "candidates_in": len(candidates),
            "already_located": 0,
            "no_url": 0,
            "pages_attempted": 0,
            "pages_fetched": 0,
            "fetch_failures": 0,
            "crawled": 0,
            "enriched": 0,
            "rejected": 0,
            "no_location": 0,
        }

        kept: list[dict[str, Any]] = []
        # Candidates needing a crawl, grouped by website so one fetch serves
        # every record sharing the URL (a source aggregate may dedupe later).
        pending: dict[str, list[dict[str, Any]]] = {}

        for candidate in candidates:
            if candidate.get("city") or candidate.get("state"):
                # Already carries location evidence (e.g. directory crawl).
                stats["already_located"] += 1
                kept.append(candidate)
                continue
            url = candidate.get("website") or candidate.get("source_url") or ""
            if not url:
                stats["no_url"] += 1
                kept.append(candidate)
                continue
            pending.setdefault(url, []).append(candidate)

        parser = HTMLParser()

        async def _run() -> None:
            items = list(pending.items())
            async with HTTPCrawler(self._config) as crawler:
                for index, (url, group) in enumerate(items):
                    if len(kept) >= limit or stats["pages_attempted"] >= MAX_PAGES:
                        # Cap reached — everything still pending stays
                        # un-enriched (honest unknown), never dropped.
                        for _, rest_group in items[index:]:
                            kept.extend(rest_group)
                        break
                    response = await self._fetch(crawler, url, stats)
                    if response is None:
                        # Unreachable — keep the candidate un-enriched.
                        kept.extend(group)
                        continue
                    stats["crawled"] += 1
                    self._consider(
                        url,
                        response.text,
                        parser.parse(response.text, base_url=response.url),
                        group=group,
                        industry=industry,
                        stats=stats,
                        kept=kept,
                    )

        asyncio.run(_run())

        stats["candidates_out"] = len(kept)
        logger.info(
            "WebsiteEnricher: %s — kept=%d crawled=%d enriched=%d rejected=%d "
            "(attempted=%d fetched=%d failures=%d)",
            self.source_name,
            len(kept),
            stats["crawled"],
            stats["enriched"],
            stats["rejected"],
            stats["pages_attempted"],
            stats["pages_fetched"],
            stats["fetch_failures"],
        )
        return kept, stats

    async def _fetch(
        self,
        crawler: Any,
        url: str,
        stats: dict[str, Any],
    ) -> CrawlResponse | None:
        """Fetch one candidate website; return ``None`` on any failure.

        The real HTTPCrawler returns a failed ``CrawlResponse`` for most
        errors but raises ``CrawlerRobotsBlocked`` before its try block, so
        both paths are handled. One dead site must not abort enrichment of
        the others (CLAUDE.md §12).
        """
        from app.crawlers.base import CrawlRequest

        stats["pages_attempted"] += 1
        try:
            response = await crawler.crawl(
                CrawlRequest(url=url, timeout=self._config.default_timeout)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("WebsiteEnricher: %s unreachable — %s", url, exc)
            stats["fetch_failures"] += 1
            return None
        if not response.successful:
            stats["fetch_failures"] += 1
            return None
        if not response.text:
            # Reachable but empty — not a fetch failure; nothing to extract.
            return None
        stats["pages_fetched"] += 1
        return response

    def _consider(
        self,
        url: str,
        html: str,
        parsed: ParsedPage,
        *,
        group: list[dict[str, Any]],
        industry: str,
        stats: dict[str, Any],
        kept: list[dict[str, Any]],
    ) -> None:
        """Extract + classify one page, then fill every candidate sharing it.

        The classifier is the accept/reject gate (Blueprint §4 line 157),
        identical to DirectoryCrawlSource: a page the extractor cannot name,
        or that the classifier rejects (manufacturer, aggregator, login
        page), is dropped — a search result that points at Yelp is never a
        company. ``location_hint`` is deliberately absent so the query can
        never leak into page evidence.
        """
        profile = self._extractor.extract(
            url,
            html,
            title=parsed.title,
            description=parsed.description,
            context={"industry_hint": industry},
        )
        classification = self._classifier.classify(
            name=profile.name,
            title=parsed.title,
            description=profile.industry_focus or parsed.description,
            url=url,
            industry_hint=industry,
        )
        if not classification["accepted"] or profile.is_rejected or not profile.name:
            stats["rejected"] += len(group)
            logger.debug(
                "WebsiteEnricher: rejected %s (%s)",
                url,
                classification.get("reject_reason")
                or profile.rejected_reason
                or "no name",
            )
            return

        for candidate in group:
            got_location = self._apply(candidate, profile, classification, url)
            if not got_location:
                stats["no_location"] += 1
            else:
                stats["enriched"] += 1
            kept.append(candidate)

    def _apply(
        self,
        candidate: dict[str, Any],
        profile: CompanyProfile,
        classification: dict[str, Any],
        url: str,
    ) -> bool:
        """Fill a candidate from REAL page evidence; return whether located.

        Only the fields the connector's Step-3 filter, AcceptanceGate and
        ``_build_result`` consume are written. ``address`` carries the clean
        ``"City, ST"`` evidence LocationVerifier needs to VERIFY the
        location (a raw city/state on the record is only a claim — Phase 2
        Step 4). Returns True when city AND state are now page-derived.
        """
        candidate["company_name"] = profile.name or candidate.get("company_name", "")
        candidate["city"] = profile.city or ""
        candidate["state"] = profile.state or ""
        candidate["address"] = profile.address or (
            f"{profile.city}, {profile.state}"
            if profile.city and profile.state
            else candidate.get("address", "")
        )
        candidate["trade_category"] = (
            classification.get("trade_category")
            or profile.trade_category
            or candidate.get("trade_category", "")
        )
        if profile.industry_focus:
            candidate["industry_focus"] = profile.industry_focus
        host = self._host_key(url)
        previous = candidate.get("data_provenance", "")
        candidate["data_provenance"] = (
            f"{previous}+enrich:{host}" if host else f"{previous}+enrich"
        )
        candidate["enrichment"] = {"url": url, "status": "success"}
        return bool(candidate["city"] and candidate["state"])

    @staticmethod
    def _host_key(url: str) -> str:
        """Normalized host without the ``www.`` prefix (for provenance)."""
        try:
            return (urlparse(url).netloc or "").lower().removeprefix("www.")
        except ValueError:
            return ""
