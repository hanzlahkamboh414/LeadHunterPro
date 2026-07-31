"""Public-source search for construction estimating companies.

Searches multiple providers (Google, Bing, DuckDuckGo) for company data.
Each provider implements the ``CompanySearchProvider`` interface.
All providers may return CAPTCHA pages from certain environments;
in that case the pipeline returns an empty list with a clear error log.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from app.engines.discovery.company.company_models import (
    CompanyDiscoveryResult,
    DiscoveryMetrics,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract provider interface
# ---------------------------------------------------------------------------


class CompanySearchProvider(ABC):
    """Abstract base class for company search providers."""

    @abstractmethod
    def search(self, query: str, page: int, limit: int) -> tuple[list[dict], bool]:
        """Search and return (results, has_more).

        Args:
            query: Search query string.
            page: Page number (1-based).
            limit: Max results per page.

        Returns:
            (list of {title, url, snippet} dicts, has_more_pages bool)
        """
        ...

    @staticmethod
    def _is_captcha(html: str) -> bool:
        """Detect whether the response is a CAPTCHA/block page."""
        low = html.lower()
        return any(w in low for w in ("captcha", "unusual traffic", "verification", "challenge"))


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


class DuckDuckGoSearchProvider(CompanySearchProvider):
    """Search DuckDuckGo HTML endpoint.

    Note: DDG also serves CAPTCHAs from certain IPs.  Detection is handled
    by ``_is_captcha``; callers should fall back gracefully.
    """

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    def search(self, query: str, page: int, limit: int) -> tuple[list[dict], bool]:
        url = f"https://duckduckgo.com/html/?q={quote(query)}"
        return self._fetch(url)

    def _fetch(self, url: str) -> tuple[list[dict], bool]:
        try:
            resp = requests.get(url, headers=self._HEADERS, timeout=15, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("DDG search failed: %s", exc)
            return [], False

        html = resp.text
        if self._is_captcha(html):
            logger.info("DDG returned CAPTCHA — provider unavailable")
            return [], False

        soup = BeautifulSoup(html, "html.parser")
        results: list[dict] = []
        for result in soup.select("result--homepage"):
            a = result.select_one("a.result__a")
            if not a:
                continue
            title = a.get_text(strip=True)
            href = a.get("href", "")
            m = re.search(r"uddg=([^&\"]+)", href)
            if m:
                href = requests.compat.unquote(m.group(1))
            text_el = result.select_one("result__snippet")
            snippet = text_el.get_text(strip=True) if text_el else ""
            if title and href:
                results.append({"title": title, "url": href, "snippet": snippet})
        return results, len(results) >= 10


class GoogleSearchProvider(CompanySearchProvider):
    """Search Google SERP via headless request."""

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    def search(self, query: str, page: int, limit: int) -> tuple[list[dict], bool]:
        start = (page - 1) * 10
        url = f"https://www.google.com/search?q={quote(query)}&num=20&tbs=qdr:y&start={start}"
        return self._fetch(url)

    def _fetch(self, url: str) -> tuple[list[dict], bool]:
        try:
            resp = requests.get(url, headers=self._HEADERS, timeout=15, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Google search failed: %s", exc)
            return [], False

        html = resp.text
        if self._is_captcha(html):
            logger.info("Google returned CAPTCHA — provider unavailable")
            return [], False

        soup = BeautifulSoup(html, "html.parser")
        results: list[dict] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/url\?q=(https?%3A//[^&]+)", href)
            if not m:
                continue
            raw_url = requests.compat.unquote(m.group(1))
            title = a.get_text(strip=True)
            if title and len(title) > 3:
                results.append({"title": title, "url": raw_url})
        return results, len(results) >= 5


class BingSearchProvider(CompanySearchProvider):
    """Search Bing SERP via headless request."""

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    def search(self, query: str, page: int, limit: int) -> tuple[list[dict], bool]:
        first = ((page - 1) * 10) + 1
        url = f"https://www.bing.com/search?q={quote(query)}&first={first}&count=20"
        return self._fetch(url)

    def _fetch(self, url: str) -> tuple[list[dict], bool]:
        try:
            resp = requests.get(url, headers=self._HEADERS, timeout=15, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Bing search failed: %s", exc)
            return [], False

        html = resp.text
        if self._is_captcha(html):
            logger.info("Bing returned CAPTCHA — provider unavailable")
            return [], False

        soup = BeautifulSoup(html, "html.parser")
        results: list[dict] = []
        for li in soup.select("li.b_ans, li.b_algo, div.b_algo"):
            a = li.select_one("a[href]")
            if not a or not a.get("href"):
                continue
            href = a["href"]
            m = re.search(r"(/url\?q=)(https?%3A//[^&]+)", href)
            if m:
                href = requests.compat.unquote(m.group(2))
            title = a.get_text(strip=True)
            snippet_el = li.select_one("p.sb_content, span.b_caption-snippet")
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            if title and len(title) > 2:
                results.append({"title": title, "url": href, "snippet": snippet})
        return results, len(results) >= 5


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def search_companies(
    industry: str,
    location: str,
    limit: int = 100,
    max_pages: int = 3,
) -> tuple[list[CompanyDiscoveryResult], DiscoveryMetrics]:
    """Search public sources for construction-estimating companies.

    Tries DuckDuckGo first, then Google, then Bing.  All providers may
    return CAPTCHA pages from headless/server environments — in that case
    the pipeline returns an empty list with a clear warning in the logs.
    No fabricated or seed data is ever returned.

    Args:
        industry: e.g. "Construction Estimating".
        location: e.g. "Dallas Texas USA".
        limit: Maximum number of results to return.
        max_pages: Search-result pages to scan per provider.

    Returns:
        A tuple of (discoveries, metrics).
    """
    metrics = DiscoveryMetrics()
    all_raw: list[dict] = []
    location_clause = f"in {location}" if location.strip() else ""
    base_query = f"{industry} {location_clause}"

    logger.info("Searching for: %r", base_query)

    providers: list[CompanySearchProvider] = [
        DuckDuckGoSearchProvider(),
        GoogleSearchProvider(),
        BingSearchProvider(),
    ]

    for provider in providers:
        source_name = type(provider).__name__.replace("SearchProvider", "").lower()
        logger.info("Trying provider: %s", source_name)
        provider_results: list[dict] = []

        for page in range(1, max_pages + 1):
            try:
                results, has_more = provider.search(base_query, page, limit)
            except Exception as exc:
                logger.warning("%s page %d failed: %s", source_name, page, exc)
                continue

            for r in results:
                url = r.get("url", "")
                if ".wikipedia.org" in url or ".gov/" in url or ".edu/" in url:
                    continue
                provider_results.append({
                    "title": r.get("title", ""),
                    "url": url,
                    "snippet": r.get("snippet", ""),
                    "source": source_name,
                })
            if not has_more:
                break

        if provider_results:
            logger.info("%s returned %d results", source_name, len(provider_results))
            all_raw.extend(provider_results)
            if len(all_raw) >= limit:
                break
        else:
            logger.info("%s returned no results (CAPTCHA or network error)", source_name)

    metrics.total_found = len(all_raw)
    logger.info("Total raw results: %d", metrics.total_found)

    discoveries: list[CompanyDiscoveryResult] = []
    for item in all_raw:
        name = _extract_company_name(item["title"], item.get("snippet", ""))
        if not name:
            continue
        source = item["source"]
        reason = f"Found via {source} SERP search result"
        discoveries.append(CompanyDiscoveryResult(
            company_name=name,
            website=item["url"],
            source=source,  # type: ignore[arg-type]
            confidence=0.6 if item.get("snippet") else 0.4,
            source_url=item["url"],
            discovery_reason=reason,
        ))
        if len(discoveries) >= limit:
            break

    return discoveries, metrics


def _extract_company_name(title: str, snippet: str) -> str:
    """Best-effort company name extraction from a SERP title/snippet.

    Strips common suffixes and filler text to get the core business name.
    """
    text = title or snippet
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"[-–—]\s*.*$", "", text)
    text = re.sub(r"[\|/••]+.*$", "", text)
    name = text.strip()
    if not name or len(name) < 3:
        words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'][A-Za-zÀ-ÖØ-öø-ÿ'-]{2,}", title or snippet)
        name = " ".join(words[:3]) if words else ""
    return name[:100]
