"""Page retrieval for Direct Website Discovery.

Units 1–7 kept ``app.discovery.website`` a leaf: no ``app.crawlers``
import, therefore no ``aiohttp``. Unit 8 finally needs real pages, and
this module is the single place that dependency is allowed to land.

The split is deliberate. :class:`PageFetcher` is the contract the plugin
depends on — one method, ``fetch(url) -> PageContent | None``. The plugin
never constructs a :class:`~app.crawlers.base.CrawlRequest`, never touches
the parser, and never awaits anything. It orchestrates; this fetches.
Swapping in a headless-browser fetcher, a cached fetcher, or a stub in a
test is then a constructor argument rather than a change to discovery
logic (CLAUDE.md §4).

Why ``fetch`` is synchronous:
    ``BaseDiscoveryPlugin.discover`` is synchronous, as are
    ``BaseSource.discover`` and ``CandidateGenerator.generate``. Rather
    than make the plugin async — which would force the whole discovery
    layer async — the async boundary is absorbed here, in the one
    component that actually performs I/O.

    :class:`CrawlerPageFetcher` therefore calls ``asyncio.run``. That is
    safe from a sync caller and **raises** if invoked from inside a
    running event loop (e.g. an async FastAPI route). That is a real
    constraint, not an oversight: it is checked explicitly below and
    reported as a clear error rather than a confusing one from deep
    inside asyncio.

Why a failed fetch returns ``None`` rather than raising:
    One unreachable company website is normal — sites go down, DNS
    fails, robots.txt disallows. That must not abort discovery of the
    other candidates. The plugin counts the ``None`` results and, if
    *every* candidate failed, reports ``UNAVAILABLE`` rather than
    ``EMPTY`` — the distinction between "nothing is there" and "I could
    not look", which CLAUDE.md §12 forbids collapsing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.discovery.website.extractors import PageContent

logger = logging.getLogger(__name__)

#: Seconds to wait for a single page before giving up.
DEFAULT_TIMEOUT = 20


@runtime_checkable
class PageFetcher(Protocol):
    """Retrieves and parses one page.

    The whole of the plugin's dependency on the network. Implementations
    turn a candidate URL into something satisfying
    :class:`~app.discovery.website.extractors.PageContent`, or report
    that they could not.
    """

    def fetch(self, url: str) -> PageContent | None:
        """Retrieve *url* and return its parsed content.

        Implementations must not raise for an unreachable page — return
        ``None`` so the caller can distinguish a failed fetch from a page
        that simply carried nothing.

        Args:
            url: Absolute URL to retrieve.

        Returns:
            Parsed page content, or ``None`` if it could not be fetched.
        """
        ...


class CrawlerPageFetcher:
    """Default fetcher: HTTPCrawler + HTMLParser.

    Reuses the production crawler wholesale (CLAUDE.md §14) — robots.txt,
    rate limiting, retry, caching and session reuse all come from
    :class:`~app.crawlers.http_crawler.HTTPCrawler` unchanged. Nothing
    about fetching is reimplemented here; this class only bridges the
    async crawler to the synchronous discovery layer and hands the HTML
    to the existing parser.

    ``ParsedPage`` satisfies the ``PageContent`` Protocol structurally,
    so no adapter is needed between the parser and the extractors.
    """

    def __init__(self, *, timeout: int = DEFAULT_TIMEOUT) -> None:
        """Configure the fetcher.

        Args:
            timeout: Per-request timeout in seconds.
        """
        self._timeout = timeout

    def fetch(self, url: str) -> PageContent | None:
        """Crawl *url* and parse the result.

        Args:
            url: Absolute URL to retrieve.

        Returns:
            The parsed page, or ``None`` if the crawl failed, was blocked
            by robots.txt, or returned a non-2xx status.

        Raises:
            RuntimeError: If called from inside a running event loop.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # No running loop — the expected, supported case.
        else:
            raise RuntimeError(
                "CrawlerPageFetcher.fetch() is synchronous and cannot be "
                "called from a running event loop. Call it from a worker "
                "thread, or supply an async-aware PageFetcher instead."
            )

        return asyncio.run(self._fetch(url))

    async def _fetch(self, url: str) -> PageContent | None:
        """Perform the crawl and parse on the event loop."""
        # Imported here, not at module scope: app/crawlers/__init__ eagerly
        # imports http_crawler and session_manager, so a top-level import
        # would pull aiohttp into every module that merely imports this
        # package's Protocol.
        from app.crawlers.base import CrawlRequest
        from app.crawlers.html_parser import HTMLParser
        from app.crawlers.http_crawler import HTTPCrawler

        crawler = HTTPCrawler()
        try:
            response = await crawler.crawl(
                CrawlRequest(url=url, timeout=self._timeout)
            )
        except Exception as exc:  # noqa: BLE001
            # The crawler already retried. A failure here is this one page
            # being unreachable, which must not abort the other candidates.
            logger.warning("PageFetcher: %s unreachable — %s", url, exc)
            return None
        finally:
            await crawler.close()

        if not response.successful:
            logger.warning(
                "PageFetcher: %s returned status=%s error=%r",
                url,
                response.status_code,
                response.error,
            )
            return None

        html = response.text
        if not html:
            logger.debug("PageFetcher: %s returned an empty body", url)
            return None

        return HTMLParser().parse(html, base_url=response.url)

    def describe(self) -> dict[str, str]:
        """Summarize this fetcher for diagnostics, per CLAUDE.md §6."""
        return {
            "name": "crawler_page_fetcher",
            "class": type(self).__name__,
            "timeout": str(self._timeout),
        }
