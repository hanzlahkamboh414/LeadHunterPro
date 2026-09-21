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

**Site chrome is excluded, deterministically** (defect found live,
2026-09-18): the page text comes from ``soup.get_text()``, which includes
the ``<nav>`` and ``<footer>``, so a careers promo repeated in the global
footer arrived on every page and made four churning pages of one site look
like four independent hiring signals. A phrase carried by most of a site's
pages is the template talking, not the page, and it is dropped — see
:meth:`CompanySiteIntentPlugin._site_wide_phrases`. No model is involved;
the rule is phrase frequency across the pages already fetched. The parser
is deliberately NOT touched: it is shared with the discovery crawler, so
stripping tags there would change address, leadership and services
extraction too.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
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

#: How many pages must have been read before a repeated phrase may be called
#: site chrome. With one or two pages fetched there is no "site-wide" to
#: speak of, and suppressing a site's only page would throw away everything
#: it said.
MIN_PAGES_FOR_CHROME = 3

#: Share of the scanned pages a phrase must EXCEED to count as chrome. A
#: phrase on a bare majority of pages is the template; a phrase on one or
#: two pages is a site that happens to mention the thing twice.
CHROME_PAGE_SHARE = 0.5


def _normalize_phrase(phrase: str) -> str:
    """Case- and whitespace-insensitive phrase key, for cross-page counting."""
    return " ".join(phrase.lower().split())


@dataclass(frozen=True)
class _PhraseMatch:
    """One signal phrase matched on one page.

    ``phrase`` is normalized so that the same sentence rendered with
    different spacing or capitalisation on different pages counts as one
    phrase — which is exactly what makes the footer in a shared template
    detectable. ``evidence.source_url`` is the page it was read from.
    """

    kind: IntentEvidenceType
    phrase: str
    evidence: IntentEvidence


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

        # Every page is read BEFORE any evidence is built, because whether a
        # phrase is a signal depends on how many of the OTHER pages carry it.
        per_page: list[list[_PhraseMatch]] = []
        failures = 0
        for url in candidates:
            page = self._fetcher.fetch(url)
            if page is None:
                failures += 1
                continue
            per_page.append(self._matches_for_page(page))
        fetched = len(per_page)

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

        matches = [match for page_matches in per_page for match in page_matches]
        site_wide = self._site_wide_phrases(matches, fetched)
        chrome_hits = [m for m in matches if (m.kind, m.phrase) in site_wide]

        evidence: list[IntentEvidence] = []
        for page_matches in per_page:
            kept_kinds: set[IntentEvidenceType] = set()
            for match in page_matches:
                if (match.kind, match.phrase) in site_wide:
                    continue  # the template talking, not the page
                if match.kind in kept_kinds:
                    continue  # one evidence per kind per page
                kept_kinds.add(match.kind)
                evidence.append(match.evidence)

        metadata: dict[str, Any] = {
            "source": self.name,
            "pages_fetched": fetched,
            "failures": failures,
            # Suppression is never silent: a reader must be able to see that
            # something was filtered, and what. ``chrome_matches`` counts
            # matches carrying a chrome phrase — an upper bound on dropped
            # rows, not the exact count, since a page that also said something
            # in its own words still contributed its one evidence row.
            "chrome_phrases": sorted({m.phrase for m in chrome_hits}),
            "chrome_matches": len(chrome_hits),
        }
        if not evidence:
            if chrome_hits:
                metadata["note"] = "only site-wide chrome matched"
            return SourceStatus.EMPTY, [], metadata
        return SourceStatus.SUCCESS, evidence, metadata

    # -- signal detection -------------------------------------------------

    def _matches_for_page(self, page: Any) -> list[_PhraseMatch]:
        """Every distinct signal phrase on *page*, in pattern order.

        Distinct by PHRASE, not by kind. A page that says both "now hiring"
        and "join our team" yields two hiring phrases, and that is what
        makes :meth:`_site_wide_phrases` able to throw away the footer one
        while keeping the real one. The evidence finally emitted is still
        one per kind per page (``collect_evidence`` picks), but the choice
        of which phrase to cite has to be able to see all of them.
        """
        text = " ".join(filter(None, [page.title, page.description, page.text_content]))
        found: list[_PhraseMatch] = []
        for kind, pattern in SIGNAL_PATTERNS.items():
            seen: set[str] = set()
            for match in pattern.finditer(text):
                phrase = _normalize_phrase(match.group(0))
                if not phrase or phrase in seen:
                    continue
                seen.add(phrase)
                found.append(
                    _PhraseMatch(
                        kind=IntentEvidenceType(kind),
                        phrase=phrase,
                        evidence=IntentEvidence(
                            type=IntentEvidenceType(kind),
                            source_url=page.url,
                            snippet=self._snippet_around(text, match),
                            source="company_site",
                        ),
                    )
                )
        return found

    @staticmethod
    def _site_wide_phrases(
        matches: list[_PhraseMatch], pages_scanned: int
    ) -> set[tuple[IntentEvidenceType, str]]:
        """The ``(kind, phrase)`` pairs that are site chrome, not content.

        A phrase repeated across most of a site's pages is the template
        talking: the careers promo in the global footer that turned four
        Turner pages into "hiring" evidence while the page bodies said
        nothing about hiring. Deterministic by construction — frequency
        across pages, no model, no scoring — and it refuses to rule on a
        sample too small to mean anything.
        """
        if pages_scanned < MIN_PAGES_FOR_CHROME:
            return set()
        pages_per_phrase: dict[tuple[IntentEvidenceType, str], set[str]] = {}
        for match in matches:
            key = (match.kind, match.phrase)
            pages_per_phrase.setdefault(key, set()).add(match.evidence.source_url)
        threshold = pages_scanned * CHROME_PAGE_SHARE
        return {key for key, urls in pages_per_phrase.items() if len(urls) > threshold}

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
