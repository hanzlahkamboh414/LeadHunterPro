"""Public-source search for construction estimating companies.

Searches Google and Bing using headless-style requests.  Returns raw
(unvalidated) company dictionaries with name, website, location hints,
and source attribution.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from app.engines.discovery.company.company_models import (
    CompanyDiscoveryResult,
    DiscoveryMetrics,
)

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Patterns that identify construction-estimating related business names.
_CONSTRUCTION_KEYWORDS = re.compile(
    r"(construction|estimat|contractor|builder|remodel|renovat|general "
    r"contractor|civil eng|develop|home improv)",
    re.IGNORECASE,
)


def _extract_snippet_text(soup: BeautifulSoup) -> str:
    """Return visible page text (stripped)."""
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)[:500]


def _parse_google_results(html: str, query: str) -> list[dict]:
    """Parse Google SERP HTML for company name + URL pairs."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    # Google <a> elements with href containing web URLs.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Google wraps organic results in /url?q=...
        m = re.search(r"/url\?q=(https?%3A//[^&]+)", href)
        if not m:
            continue
        raw_url = requests.compat.unquote(m.group(1))
        title = a.get_text(strip=True)
        if title and len(title) > 3:
            results.append({"title": title, "url": raw_url})

    return results


def _parse_bing_results(html: str, query: str) -> list[dict]:
    """Parse Bing SERP HTML for company name + URL pairs."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    for li in soup.select("li.b_ans, li.b_algo, div.b_algo"):
        a = li.select_one("a[href]")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        # Bing also wraps URLs; extract the actual destination.
        m = re.search(r"(/url\?q=)(https?%3A//[^&]+)", href)
        if m:
            href = requests.compat.unquote(m.group(2))
        title = a.get_text(strip=True)
        snippet_el = li.select_one("p.sb_content, span.b_caption-snippet")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        if title and len(title) > 2:
            results.append({"title": title, "url": href, "snippet": snippet})

    return results


def _search_query(query: str) -> str:
    """URL-encode a search query string."""
    return quote(query)


def _fetch_page(url: str, timeout: int = 15) -> str | None:
    """Fetch a URL and return raw HTML, or None on failure."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def search_companies(
    industry: str,
    location: str,
    limit: int = 100,
    max_pages: int = 3,
) -> tuple[list[CompanyDiscoveryResult], DiscoveryMetrics]:
    """Search public sources for construction-estimating companies.

    Args:
        industry: e.g. "Construction Estimating".
        location: e.g. "Dallas Texas USA".
        limit: Maximum number of results to return.
        max_pages: How many search-result pages to inspect per source.

    Returns:
        A tuple of (discoveries, metrics).
    """
    metrics = DiscoveryMetrics()
    all_raw: list[dict] = []
    location_clause = f"in {location}" if location.strip() else ""
    base_query = f"{industry} {location_clause}"

    # ---- Google ----
    for page in range(1, max_pages + 1):
        url = (
            f"https://www.google.com/search?"
            f"q={_search_query(base_query)}"
            f"&num=20&tbs=qdr:y"
            f"&start={(page - 1) * 10}"
        )
        html = _fetch_page(url)
        if not html:
            metrics.errors.append(f"google: page {page} failed")
            continue
        results = _parse_google_results(html, base_query)
        for r in results:
            # Skip Wikipedia, government .gov pages (not commercial companies).
            if ".wikipedia.org" in r["url"] or ".gov/" in r["url"]:
                continue
            all_raw.append({
                "title": r["title"],
                "url": r["url"],
                "snippet": "",
                "source": "google",
            })
        if len(results) < 5:
            break

    # ---- Bing ----
    for page in range(1, max_pages + 1):
        url = (
            f"https://www.bing.com/search?"
            f"q={_search_query(base_query)}"
            f"&first={((page - 1) * 10) + 1}"
            f"&count=20"
        )
        html = _fetch_page(url)
        if not html:
            metrics.errors.append(f"bing: page {page} failed")
            continue
        results = _parse_bing_results(html, base_query)
        for r in results:
            if ".wikipedia.org" in r["url"] or ".gov/" in r["url"]:
                continue
            all_raw.append({
                "title": r["title"],
                "url": r["url"],
                "snippet": r.get("snippet", ""),
                "source": "bing",
            })
        if len(results) < 5:
            break

    metrics.total_found = len(all_raw)
    logger.info("Discovered %d raw results from public sources", metrics.total_found)

    discoveries: list[CompanyDiscoveryResult] = []
    for item in all_raw:
        name = _extract_company_name(item["title"], item["snippet"])
        if not name:
            continue
        discoveries.append(CompanyDiscoveryResult(
            company_name=name,
            website=item["url"],
            source=item["source"],
            confidence=0.6 if item["snippet"] else 0.4,
        ))
        if len(discoveries) >= limit:
            break

    return discoveries, metrics


def _extract_company_name(title: str, snippet: str) -> str:
    """Best-effort company name extraction from a SERP title/snippet.

    Strips common suffixes and filler text to get the core business name.
    """
    text = title or snippet
    # Remove parentheticals, hyphen-separated taglines.
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"[-–—]\s*.*$", "", text)
    # Remove trailing separators.
    text = re.sub(r"[\|/••]+.*$", "", text)
    name = text.strip()
    # Fallback: take first meaningful word group.
    if not name or len(name) < 3:
        words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'][A-Za-zÀ-ÖØ-öø-ÿ'-]{2,}", title or snippet)
        name = " ".join(words[:3]) if words else ""
    return name[:100]
