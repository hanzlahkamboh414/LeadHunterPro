"""Public-source search for construction estimating companies.

Provider chain (checked in priority order):
    1. Serper      (if SERPER_API_KEY is set)
    2. SerpAPI     (if SERPAPI_API_KEY is set)
    3. Google CSE  (if GOOGLE_CSE_API_KEY+ID are set)
    4. Bing        (free HTML fallback)
    5. DuckDuckGo  (free HTML fallback)

Each provider implements the ``CompanySearchProvider`` interface and
reports its own availability via ``available()`` and ``priority()``.

If ALL providers fail, the pipeline returns a structured diagnostic
dict instead of an empty list — never silently returning [].
No fabricated or seed data is ever returned.
"""

from __future__ import annotations

import logging
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from app.engines.discovery.company.company_models import (
    CompanyDiscoveryResult,
    DiscoveryMetrics,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class CompanySearchProvider(ABC):
    """Abstract base class for company search providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name (e.g. 'serper', 'bing')."""
        ...

    @property
    @abstractmethod
    def priority(self) -> int:
        """Lower number = higher priority. Checked first."""
        ...

    def available(self) -> bool:
        """Return True if this provider has the credentials/config it needs."""
        return True

    @abstractmethod
    def search(self, query: str, page: int, limit: int) -> tuple[list[dict], bool]:
        """Execute one page of search results.

        Args:
            query: Raw search query string.
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
# Provider 1: Serper (serper.dev)
# ---------------------------------------------------------------------------


class SerperSearchProvider(CompanySearchProvider):
    """Serper.dev Google SERP JSON API."""

    name = "serper"
    priority = 1
    BASE_URL = "https://google.serper.dev/search"
    _HEADERS = {
        "X-API-KEY": "",
        "Content-Type": "application/json",
    }

    def available(self) -> bool:
        return bool(os.environ.get("SERPER_API_KEY"))

    def search(self, query: str, page: int, limit: int) -> tuple[list[dict], bool]:
        api_key = os.environ.get("SERPER_API_KEY", "")
        self._HEADERS["X-API-KEY"] = api_key

        payload = {
            "q": query,
            "num": min(limit, 20),
            "gl": "us",
            "hl": "en",
            "page": page,
        }
        try:
            resp = requests.post(self.BASE_URL, headers=self._HEADERS, json=payload, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("%s: request failed: %s", self.name, exc)
            return [], False

        results: list[dict] = []
        for item in data.get("organic", []):
            title = (item.get("title") or "").strip()
            url = (item.get("link") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            if title and url:
                results.append({"title": title, "url": url, "snippet": snippet})

        total = int(data.get("information", {}).get("total_results", 0))
        return results, len(results) >= 10 and page * 10 < total


# ---------------------------------------------------------------------------
# Provider 2: SerpAPI
# ---------------------------------------------------------------------------


class SerpAPISearchProvider(CompanySearchProvider):
    """SerpAPI Google/Bing SERP JSON API."""

    name = "serpapi"
    priority = 2
    BASE_URL = "https://serpapi.com/search"
    _HEADERS = {"User-Agent": "LeadHunterPro/1.0"}

    def available(self) -> bool:
        return bool(os.environ.get("SERPAPI_API_KEY"))

    def search(self, query: str, page: int, limit: int) -> tuple[list[dict], bool]:
        api_key = os.environ.get("SERPAPI_API_KEY")
        params = {
            "q": query,
            "engine": "google",
            "num": min(limit, 20),
            "start": ((page - 1) * 20) + 1,
            "api_key": api_key,
            "gl": "us",
            "hl": "en",
        }
        try:
            resp = requests.get(self.BASE_URL, headers=self._HEADERS, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("%s: request failed: %s", self.name, exc)
            return [], False

        organic: list[dict] = []
        for item in data.get("organic_results", []):
            title = (item.get("title") or "").strip()
            url = (item.get("link") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            if title and url:
                organic.append({"title": title, "url": url, "snippet": snippet})

        next_page = data.get("next_page_token")
        return organic, next_page is not None


# ---------------------------------------------------------------------------
# Provider 3: Google Custom Search Engine
# ---------------------------------------------------------------------------


class GoogleCSEProvider(CompanySearchProvider):
    """Google Custom Search JSON API."""

    name = "google_cse"
    priority = 3
    BASE_URL = "https://www.googleapis.com/customsearch/v1"

    def available(self) -> bool:
        key = os.environ.get("GOOGLE_CSE_API_KEY")
        cx = os.environ.get("GOOGLE_CSE_ID")
        return bool(key and cx)

    def search(self, query: str, page: int, limit: int) -> tuple[list[dict], bool]:
        api_key = os.environ.get("GOOGLE_CSE_API_KEY", "")
        cx = os.environ.get("GOOGLE_CSE_ID", "")
        start_idx = ((page - 1) * 10) + 1
        params = {
            "q": query,
            "key": api_key,
            "cx": cx,
            "start": start_idx,
            "num": min(limit, 10),
        }
        try:
            resp = requests.get(self.BASE_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("%s: request failed: %s", self.name, exc)
            return [], False

        results: list[dict] = []
        for item in data.get("items", []):
            title = (item.get("title") or "").strip()
            url = (item.get("link") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            if title and url:
                results.append({"title": title, "url": url, "snippet": snippet})

        total = int(data.get("searchInformation", {}).get("totalResults", 0))
        return results, start_idx + len(results) <= total


# ---------------------------------------------------------------------------
# Provider 4: Bing (free HTML)
# ---------------------------------------------------------------------------


class BingSearchProvider(CompanySearchProvider):
    """Search Bing SERP via headless request (no API key needed)."""

    name = "bing"
    priority = 4
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
            logger.warning("%s: request failed: %s", self.name, exc)
            return [], False

        html = resp.text
        if self._is_captcha(html):
            logger.info("%s: CAPTCHA detected — provider unavailable", self.name)
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
# Provider 5: DuckDuckGo (free HTML)
# ---------------------------------------------------------------------------


class DuckDuckGoSearchProvider(CompanySearchProvider):
    """Search DuckDuckGo HTML endpoint (no API key needed)."""

    name = "duckduckgo"
    priority = 5
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
            logger.warning("%s: request failed: %s", self.name, exc)
            return [], False

        html = resp.text
        if self._is_captcha(html):
            logger.info("%s: CAPTCHA detected — provider unavailable", self.name)
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


# ---------------------------------------------------------------------------
# Diagnostic model
# ---------------------------------------------------------------------------


class DiscoveryDiagnostic:
    """Structured explanation of why no companies were discovered."""

    def __init__(
        self,
        providers_attempted: list[str],
        providers_skipped: list[dict[str, str]],
        errors: dict[str, str],
    ) -> None:
        self.providers_attempted = providers_attempted
        self.providers_skipped = providers_skipped
        self.errors = errors

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict suitable for inclusion in the API response."""
        recommendation = "Configure at least one of: SERPER_API_KEY, SERPAPI_API_KEY, "
        recommendation += "or GOOGLE_CSE_API_KEY+GOOGLE_CSE_ID for reliable results."
        if self.providers_attempted:
            recommendation += (
                f" Free providers ({', '.join(self.providers_attempted)}) are blocked by CAPTCHA "
                "in this environment."
            )
        return {
            "status": "no_provider_available",
            "reason": (
                f"All {len(self.providers_attempted)} live providers were attempted but none "
                f"returned results. Skipped {len(self.providers_skipped)} providers (not configured)."
            ),
            "providers_attempted": self.providers_attempted,
            "providers_skipped": self.providers_skipped,
            "errors": self.errors,
            "recommendation": recommendation,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def search_companies(
    industry: str,
    location: str,
    limit: int = 100,
    max_pages: int = 3,
) -> tuple[list[CompanyDiscoveryResult], DiscoveryMetrics, DiscoveryDiagnostic | None]:
    """Search public sources for construction-estimating companies.

    Provider chain (priority order, each checked only if available):
        1. Serper   — if SERPER_API_KEY is set
        2. SerpAPI  — if SERPAPI_API_KEY is set
        3. Google CSE — if GOOGLE_CSE_API_KEY + GOOGLE_CSE_ID are set
        4. Bing     — free HTML fallback
        5. DuckDuckGo — free HTML fallback

    Never fabricates or seeds data. If all providers fail a structured
    diagnostic is returned describing exactly what was attempted.

    Args:
        industry: e.g. "Construction Estimating".
        location: e.g. "Dallas Texas USA".
        limit: Maximum number of results to return.
        max_pages: Pages to scan per provider.

    Returns:
        (discoveries, metrics, diagnostic)
        diagnostic is None only when results were successfully found.
    """
    metrics = DiscoveryMetrics()
    all_raw: list[dict] = []
    location_clause = f"in {location}" if location.strip() else ""
    base_query = f"{industry} {location_clause}"

    logger.info("Discovery search started: query=%r industry=%r location=%r limit=%d", base_query, industry, location, limit)

    # All known providers sorted by priority.
    all_providers: list[CompanySearchProvider] = sorted(
        [SerperSearchProvider(), SerpAPISearchProvider(), GoogleCSEProvider(), BingSearchProvider(), DuckDuckGoSearchProvider()],
        key=lambda p: p.priority,
    )

    providers_attempted: list[str] = []
    providers_skipped: list[dict[str, str]] = []
    errors: dict[str, str] = {}

    for provider in all_providers:
        if not provider.available():
            providers_skipped.append({
                "provider": provider.name,
                "reason": "not configured (missing required env var or credentials)",
            })
            logger.info("%s: skipped — not configured", provider.name)
            continue

        logger.info("%s: available, attempting search", provider.name)
        providers_attempted.append(provider.name)
        provider_results: list[dict] = []

        for page in range(1, max_pages + 1):
            t0 = time.monotonic()
            try:
                results, has_more = provider.search(base_query, page, limit)
                elapsed = time.monotonic() - t0
                logger.info(
                    "%s page %d: %d results in %.2fs",
                    provider.name, page, len(results), elapsed,
                )
            except Exception as exc:
                elapsed = time.monotonic() - t0
                logger.warning(
                    "%s page %d failed after %.2fs: %s",
                    provider.name, page, elapsed, exc,
                )
                errors[provider.name] = f"page {page} error: {exc}"
                continue

            for r in results:
                url = r.get("url", "")
                if ".wikipedia.org" in url or ".gov/" in url or ".edu/" in url:
                    continue
                provider_results.append({
                    "title": r.get("title", ""),
                    "url": url,
                    "snippet": r.get("snippet", ""),
                    "source": provider.name,
                })
            if not has_more:
                break

        if provider_results:
            logger.info("%s: %d valid results collected", provider.name, len(provider_results))
            all_raw.extend(provider_results)
            if len(all_raw) >= limit:
                break
        else:
            logger.info("%s: no results returned", provider.name)
            if provider.name not in errors:
                errors[provider.name] = "returned no results"

    # Build final discoveries
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

    metrics.total_found = len(all_raw)
    logger.info("Search complete: %d raw, %d final discoveries", metrics.total_found, len(discoveries))

    diagnostic: DiscoveryDiagnostic | None = None
    if not discoveries:
        diagnostic = DiscoveryDiagnostic(
            providers_attempted=providers_attempted,
            providers_skipped=providers_skipped,
            errors=errors,
        )
        logger.warning("No companies discovered — diagnostic: %s", diagnostic.to_dict()["reason"])

    return discoveries, metrics, diagnostic


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
