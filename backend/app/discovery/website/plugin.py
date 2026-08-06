"""Direct Website Discovery Plugin (Unit 8).

The component that finally joins Units 1–7 into discovery: ask the
generators for candidate company URLs, drop duplicates, fetch each page,
run the extractors over it, and emit company records with their evidence.

**This module orchestrates and nothing else.** It does not search, fetch,
parse, extract, deduplicate URLs or score — each of those already has an
owner, and the plugin's job is to call them in the right order and report
honestly on what happened:

    CandidateGenerator  → candidate URLs        (Unit 6 + generators)
    DuplicateURLFilter  → unique URLs           (Unit 2)
    PageFetcher         → PageContent           (page_fetcher)
    ExtractorManager    → ExtractionResults     (2.3B)
    this module         → company dicts + status

Every collaborator is injected, so no concrete provider is wired into the
discovery path (CLAUDE.md §4). A plugin constructed with no generators
discovers nothing and says so loudly — it never invents data and never
silently reaches for a fixture (§1, §5). Bridge data, if it is ever
wanted, is a generator named ``fixture`` whose name appears on every
candidate it proposes, not a hidden branch in here.

Status semantics, per ``BaseDiscoveryPlugin``:

    ``SUCCESS``      at least one company was extracted
    ``EMPTY``        ran correctly, found nothing
    ``UNAVAILABLE``  could not consult the sources at all — every
                     generator raised, or every candidate page failed
                     to fetch
    ``ERROR``        unexpected internal failure

The ``EMPTY`` / ``UNAVAILABLE`` split is the point. "No roofers in
Dallas" and "I could not reach anything" are different facts, and
reporting the second as the first is the hidden failure CLAUDE.md §12
forbids.

Note on ``SourceStatus.FAILED``: ``candidates.py`` instructs Unit 8 to map
a raising generator to that member. It does not exist — ``SourceStatus``
declares only SUCCESS/EMPTY/UNAVAILABLE/ERROR. A generator that raises
means its origin could not be consulted, which is ``UNAVAILABLE`` by the
framework's own definition. The enum is left untouched.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.discovery.plugins.base_plugin import BaseDiscoveryPlugin, PluginCapability
from app.discovery.sources.status import SourceStatus
from app.discovery.website.evidence import ExtractedField
from app.discovery.website.extractor_manager import ExtractorManager
from app.discovery.website.plugin_config_view import make_config_view
from app.discovery.website.url_filter import DuplicateURLFilter

if TYPE_CHECKING:
    from app.discovery.plugins.plugin_config import PluginConfig
    from app.discovery.website.candidates import Candidate, CandidateGenerator
    from app.discovery.website.extractors import ExtractionResult
    from app.discovery.website.page_fetcher import PageFetcher

logger = logging.getLogger(__name__)

#: Hard ceiling on pages fetched per run, regardless of *limit*. Crawling
#: is the expensive step; an unbounded candidate list must not translate
#: into an unbounded crawl.
DEFAULT_MAX_PAGES = 25

#: Fields whose results collapse to a single best value on the company
#: record. The rest stay lists, because a company genuinely has several
#: services, leaders and social profiles.
_SINGULAR_FIELDS = {
    ExtractedField.COMPANY_NAME: "name",
    ExtractedField.PHONE: "phone",
    ExtractedField.EMAIL: "email",
    ExtractedField.ADDRESS: "address",
}


class DirectWebsiteDiscoveryPlugin(BaseDiscoveryPlugin):
    """Discovers companies by visiting their websites directly.

    Args:
        config: Optional runtime configuration. ``options`` may carry
            ``max_pages`` to cap how many candidate pages are fetched.
        generators: Candidate sources. Injected rather than constructed
            so no search provider is hardwired into discovery (§3, §8).
        fetcher: Page retrieval. Defaults to ``CrawlerPageFetcher``,
            constructed lazily so importing this module pulls in no
            ``aiohttp``.
        extractor_manager: Field extraction. Defaults to a stock
            ``ExtractorManager``.
    """

    name = "direct_website_discovery"
    description = "Finds companies by crawling and extracting their websites"
    priority = 20
    capabilities = (
        PluginCapability.COMPANY_DISCOVERY,
        PluginCapability.WEBSITE_DISCOVERY,
        PluginCapability.EMAIL_DISCOVERY,
        PluginCapability.PHONE_DISCOVERY,
        PluginCapability.LEADERSHIP_DISCOVERY,
        PluginCapability.SOCIAL_DISCOVERY,
    )

    def __init__(
        self,
        config: PluginConfig | None = None,
        *,
        generators: list[CandidateGenerator] | None = None,
        fetcher: PageFetcher | None = None,
        extractor_manager: ExtractorManager | None = None,
    ) -> None:
        super().__init__(config)
        self._generators = list(generators) if generators else []
        self._fetcher = fetcher
        self._extractors = extractor_manager or ExtractorManager()

        options = getattr(self.config, "options", None) or {}
        self._options = make_config_view(dict(options))
        self._max_pages = int(self._options.get("max_pages", DEFAULT_MAX_PAGES))

        if not self._generators:
            # §5: an empty registry is reported, never silently tolerated.
            logger.warning(
                "NO CANDIDATE GENERATORS REGISTERED for plugin %r — "
                "discovery cannot run and will report UNAVAILABLE",
                self.name,
            )

    @property
    def fetcher(self) -> PageFetcher:
        """The page fetcher, built on first use.

        Deferred so that merely importing or describing the plugin does
        not construct a crawler or import ``aiohttp``.
        """
        if self._fetcher is None:
            from app.discovery.website.page_fetcher import CrawlerPageFetcher

            self._fetcher = CrawlerPageFetcher()
        return self._fetcher

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        """Run website discovery for *industry* in *location*."""
        metadata: dict[str, Any] = {
            "plugin": self.name,
            "query": {
                "industry": industry,
                "location": location,
                "limit": limit,
            },
            "generators_found": [g.generator_name for g in self._generators],
            "data_source": "live",
        }

        if not self._generators:
            metadata["fallback_reason"] = "no candidate generators registered"
            logger.error(
                "%s: NO CANDIDATE GENERATORS REGISTERED — cannot discover",
                self.name,
            )
            return SourceStatus.UNAVAILABLE, [], metadata

        try:
            return self._run(industry, location, limit, metadata)
        except Exception as exc:  # noqa: BLE001
            # Only genuinely unexpected failures reach here; per-generator
            # and per-page failures are handled where they occur.
            logger.exception("%s: unexpected failure", self.name)
            metadata["error"] = str(exc)
            metadata["fallback_reason"] = f"internal error: {exc}"
            return SourceStatus.ERROR, [], metadata

    def _run(
        self,
        industry: str,
        location: str,
        limit: int,
        metadata: dict[str, Any],
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        """Execute the pipeline. Split out so discover() owns error policy."""
        candidates, failures = self._generate(industry, location, limit)
        metadata["generator_failures"] = failures
        metadata["candidates_proposed"] = len(candidates)

        if failures and len(failures) == len(self._generators):
            # Every origin failed: we did not look, so we cannot say
            # "nothing is there".
            metadata["fallback_reason"] = "every candidate generator failed"
            logger.error(
                "%s: all %d generators failed", self.name, len(failures)
            )
            return SourceStatus.UNAVAILABLE, [], metadata

        unique = self._deduplicate(candidates, metadata)
        crawl_list = unique[: self._max_pages]
        metadata["pages_attempted"] = len(crawl_list)

        companies: list[dict[str, Any]] = []
        fetch_failures = 0
        rejected = 0

        for candidate in crawl_list:
            page = self.fetcher.fetch(candidate.url)
            if page is None:
                fetch_failures += 1
                continue

            results = self._extractors.extract_all(page)
            company = self._build_company(candidate, results)
            if company is None:
                rejected += 1
                continue
            companies.append(company)
            if len(companies) >= limit:
                break

        metadata["pages_fetched"] = len(crawl_list) - fetch_failures
        metadata["fetch_failures"] = fetch_failures
        metadata["companies_accepted"] = len(companies)
        metadata["companies_rejected"] = rejected

        logger.info(
            "%s: generators=%d candidates=%d unique=%d attempted=%d "
            "fetched=%d accepted=%d rejected=%d",
            self.name,
            len(self._generators),
            len(candidates),
            len(unique),
            len(crawl_list),
            metadata["pages_fetched"],
            len(companies),
            rejected,
        )

        if companies:
            return SourceStatus.SUCCESS, companies, metadata

        if crawl_list and fetch_failures == len(crawl_list):
            metadata["fallback_reason"] = "every candidate page failed to fetch"
            return SourceStatus.UNAVAILABLE, [], metadata

        metadata["fallback_reason"] = "no companies extracted from fetched pages"
        return SourceStatus.EMPTY, [], metadata

    def _generate(
        self,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[Candidate], list[dict[str, str]]]:
        """Collect candidates from every generator.

        A generator that raises is recorded and skipped: one dead origin
        must not abort the others. Per the CandidateGenerator contract,
        raising means "could not run" — which is why failures are counted
        rather than folded into an empty result.
        """
        candidates: list[Candidate] = []
        failures: list[dict[str, str]] = []

        for generator in self._generators:
            try:
                produced = generator.generate(
                    industry=industry, location=location, limit=limit
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "%s: generator %r failed — %s",
                    self.name,
                    generator.generator_name,
                    exc,
                )
                failures.append(
                    {"generator": generator.generator_name, "error": str(exc)}
                )
                continue

            logger.info(
                "%s: generator %r proposed %d candidates",
                self.name,
                generator.generator_name,
                len(produced),
            )
            candidates.extend(produced)

        return candidates, failures

    def _deduplicate(
        self,
        candidates: list[Candidate],
        metadata: dict[str, Any],
    ) -> list[Candidate]:
        """Drop candidates whose URLs were already seen.

        Cross-*generator* deduplication only — generators are forbidden
        from hiding their own duplicates, so the filter's statistics stay
        truthful (§6). Cross-*plugin* deduplication remains the
        orchestrator's job.
        """
        url_filter = DuplicateURLFilter()
        unique: list[Candidate] = []

        for candidate in candidates:
            if url_filter.accept(candidate.url) is not None:
                unique.append(candidate)

        metadata["duplicates_removed"] = url_filter.stats.duplicates
        metadata["url_filter"] = url_filter.stats.to_dict()
        return unique

    def _build_company(
        self,
        candidate: Candidate,
        results: list[ExtractionResult],
    ) -> dict[str, Any] | None:
        """Assemble one company record from a page's extraction results.

        Returns ``None`` when the page yielded no company name — a record
        with no name is not a company, and emitting one would be the
        synthetic result CLAUDE.md §12 forbids.

        Selection policy lives here, deliberately. ``EvidenceSet`` ships
        no "best value" accessor because choosing between three observed
        phone numbers is a judgement, not a property of the evidence. The
        judgement made here is: highest confidence wins, first observed
        breaks the tie.
        """
        by_field: dict[str, list[ExtractionResult]] = {}
        for result in results:
            by_field.setdefault(result.field_name, []).append(result)

        name_results = by_field.get(str(ExtractedField.COMPANY_NAME), [])
        if not name_results:
            logger.debug(
                "%s: rejecting %s — no company name extracted",
                self.name,
                candidate.url,
            )
            return None

        company: dict[str, Any] = {
            "website": candidate.url,
            "source_url": candidate.url,
            "discovered_by": candidate.generator,
            "data_source": "live",
            "bridge_mode": False,
        }

        for field_enum, key in _SINGULAR_FIELDS.items():
            found = by_field.get(str(field_enum))
            if found:
                company[key] = self._best(found).value

        company["services"] = [
            r.value for r in by_field.get(str(ExtractedField.SERVICES), [])
        ]
        company["leadership"] = [
            {"name": r.value, "title": r.attributes.get("title", "")}
            for r in by_field.get(str(ExtractedField.LEADERSHIP), [])
        ]
        company["social"] = {
            r.attributes.get("platform", "unknown"): r.value
            for r in by_field.get(str(ExtractedField.SOCIAL), [])
        }
        company["evidence"] = [r.to_dict() for r in results]

        return company

    @staticmethod
    def _best(results: list[ExtractionResult]) -> ExtractionResult:
        """Highest-confidence result; first observed wins a tie."""
        return max(results, key=lambda r: r.evidence.confidence.score)

    async def health_check(self) -> dict[str, Any]:
        """Report configuration state without performing any I/O."""
        base = await super().health_check()
        base["generators"] = [g.generator_name for g in self._generators]
        base["extractors"] = self._extractors.describe()["count"]
        base["max_pages"] = self._max_pages
        if not self._generators:
            base["healthy"] = False
            base["reason"] = "no candidate generators registered"
        return base
