"""Company-site buying-intent plugin (PROJECT_DISCOVERY).

Crawls a company's own website — the strongest free signal that it is
actively buying: a live "now hiring" banner, a current-projects page, an
expansion announcement. Each matched signal becomes an
:class:`IntentEvidence` whose ``source_url`` is the exact page it was
read from, so the evidence is traceable (lead schema hard rule #5).

Reuses the :class:`~app.discovery.website.page_fetcher.PageFetcher` seam
for all fetching (CLAUDE.md §4, §14) — the plugin never touches the
network itself, and a test can inject a stub. A failing page is skipped,
not fatal; if every candidate page fails the plugin reports
``UNAVAILABLE`` ("could not look") rather than ``EMPTY`` ("nothing
there").
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

from app.discovery.intent.base import BaseIntentPlugin
from app.discovery.plugins.base_plugin import PluginCapability
from app.discovery.sources.status import SourceStatus
from app.discovery.website.page_fetcher import CrawlerPageFetcher, PageFetcher
from app.engines.lead.lead_models import IntentEvidence, IntentEvidenceType

logger = logging.getLogger(__name__)

#: Buying-signal keyword patterns, mapped to the intent type they support.
#: Deliberately conservative: only phrases that imply ACTIVE buying
#: (current work, open hiring, expansion) qualify as evidence — a generic
#: marketing line never does.
SIGNAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "project": re.compile(
        r"currently working on|projects in progress|active projects|"
        r"under construction|currently underway|our projects include",
        re.IGNORECASE,
    ),
    "hiring": re.compile(
        r"now hiring|we are hiring|join our team|open positions|"
        r"now accepting applications|we are looking for",
        re.IGNORECASE,
    ),
    "expansion": re.compile(
        r"new location|expanding (to|into|in)|now serving|recently opened|"
        r"grand opening|opening (a|our) new",
        re.IGNORECASE,
    ),
}


class CompanySiteIntentPlugin(BaseIntentPlugin):
    """Crawl a company's website for current project/hiring/expansion signs."""

    name = "company_site"
    description = "Crawl the company website for project, hiring, and expansion signals"
    priority = 10
    capabilities = (PluginCapability.PROJECT_DISCOVERY,)

    #: Pages most likely to carry current buying signals, in crawl order.
    CANDIDATE_PAGES = (
        "/",
        "/about",
        "/projects",
        "/project",
        "/services",
        "/careers",
        "/news",
    )

    def __init__(
        self,
        page_fetcher: PageFetcher | None = None,
        config=None,
    ) -> None:
        """Initialize the plugin.

        Args:
            page_fetcher: Fetching implementation. Defaults to the real
                :class:`CrawlerPageFetcher`; tests inject a stub here
                rather than monkeypatching the network.
            config: Optional plugin configuration (see
                :class:`~app.discovery.plugins.base_plugin.BaseDiscoveryPlugin`).
        """
        super().__init__(config)
        self._fetcher = page_fetcher if page_fetcher is not None else CrawlerPageFetcher()

    def collect_evidence(
        self,
        *,
        company_name: str,
        website: str = "",
        location: str = "",
    ) -> tuple[SourceStatus, list[IntentEvidence], dict[str, Any]]:
        """Crawl the site and return one evidence item per matched signal."""
        if not website or not website.strip():
            logger.info("CompanySiteIntentPlugin: no website to crawl for %r", company_name)
            return SourceStatus.EMPTY, [], {
                "source": self.name,
                "note": "no website to crawl",
            }

        base = self._normalize_website(website)
        candidates = [urljoin(base, page) for page in self.CANDIDATE_PAGES]

        evidence: list[IntentEvidence] = []
        fetched = 0
        failures = 0
        for url in candidates:
            page = self._fetcher.fetch(url)
            if page is None:
                failures += 1
                continue
            fetched += 1
            evidence.extend(self._evidence_from_page(page))

        if fetched == 0:
            logger.warning(
                "CompanySiteIntentPlugin: no candidate page reachable for %r (%d failure(s))",
                website,
                failures,
            )
            return (
                SourceStatus.UNAVAILABLE,
                [],
                {
                    "source": self.name,
                    "note": "no candidate page reachable",
                    "failures": failures,
                },
            )
        if not evidence:
            return SourceStatus.EMPTY, [], {"source": self.name, "pages_fetched": fetched}
        return SourceStatus.SUCCESS, evidence, {
            "source": self.name,
            "pages_fetched": fetched,
            "failures": failures,
        }

    # -- signal detection -------------------------------------------------

    def _evidence_from_page(self, page: Any) -> list[IntentEvidence]:
        """One IntentEvidence per matched signal kind on *page*."""
        text = " ".join(filter(None, [page.title, page.description, page.text_content]))
        found: list[IntentEvidence] = []
        for kind, pattern in SIGNAL_PATTERNS.items():
            match = pattern.search(text)
            if match is None:
                continue
            found.append(
                IntentEvidence(
                    type=IntentEvidenceType(kind),
                    source_url=page.url,
                    snippet=self._snippet_around(text, match),
                    source="company_site",
                )
            )
        return found

    @staticmethod
    def _snippet_around(text: str, match: re.Match[str], width: int = 160) -> str:
        """A compact text window around the match, for audit."""
        start = max(0, match.start() - 40)
        end = min(len(text), start + width)
        return " ".join(text[start:end].split())

    @staticmethod
    def _normalize_website(website: str) -> str:
        """Ensure *website* is an absolute base URL without a trailing slash."""
        website = website.strip()
        if "://" not in website:
            website = f"https://{website}"
        return website.rstrip("/")
