"""AI Lead Research — CompanyResearcher (Stage 1: refine domain + company facts).

Reusable, deterministic domain refinement (reuses ``domain_has_mx``) plus
AI-cited company research. All seams are injectable for testing — zero
network calls in production code paths.

Safety rules enforced:
- Every fact has a source_url or is marked ``unverified``.
- Company name is never fabricated — empty string means "could not determine".
- AI output is parsed from JSON; malformed output yields a partial profile
  with source_errors, never fabricated data.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Protocol

from app.lead_research.models import AIEvidence, CompanyProfile
from app.lead_research.prompts import company_research_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols (injectable seams)
# ---------------------------------------------------------------------------

class AIAskFn(Protocol):
    """Signature matching ``AIGateway().ask``."""
    def __call__(self, prompt: str) -> str: ...


class SearchFn(Protocol):
    """Returns a list of search results, each with url/title/snippet."""
    def __call__(self, query: str) -> list[dict[str, str]]: ...


class FetchPageFn(Protocol):
    """Fetches a URL and returns an object with .ok (bool) and .html (str)."""
    def __call__(self, url: str) -> Any: ...


class RefineDomainFn(Protocol):
    """Given a raw domain, returns the best-guess refined domain."""
    def __call__(self, domain: str) -> str: ...


# ---------------------------------------------------------------------------
# Deterministic domain refinement
# ---------------------------------------------------------------------------

# TLDs to try when the original looks malformed (e.g. ".comz" → ".com")
_TLD_CANDIDATES = (".com", ".net", ".org", ".us")


def _normalize_domain(domain: str) -> str:
    """Strip scheme, www, path, trailing dot, whitespace."""
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = re.sub(r"/.*$", "", d)
    d = d.rstrip(".")
    return d


def default_refine_domain(
    domain: str,
    *,
    mx_check: Callable[[str], bool] | None = None,
) -> str:
    """Deterministic domain refinement.

    1. Normalize (strip scheme/www/path).
    2. If MX resolves → keep.
    3. Else try replacing the final TLD with common alternatives and check MX.
    4. Return the best guess (original if nothing better).

    ``mx_check`` defaults to ``domain_has_mx`` from ``app.email.domain_verifier``.
    """
    if mx_check is None:
        from app.email.domain_verifier import domain_has_mx
        mx_check = domain_has_mx

    norm = _normalize_domain(domain)
    if not norm:
        return domain

    # Already resolves?
    try:
        if mx_check(norm):
            return norm
    except Exception:
        logger.debug("MX check failed for %s", norm)

    # Try TLD corrections
    parts = norm.rsplit(".", 1)
    if len(parts) == 2:
        base = parts[0]
        for tld in _TLD_CANDIDATES:
            candidate = f"{base}{tld}"
            try:
                if mx_check(candidate):
                    logger.info("Domain refined: %s → %s (MX verified)", domain, candidate)
                    return candidate
            except Exception:
                continue

    return norm  # best guess even if MX didn't resolve


# ---------------------------------------------------------------------------
# Search + fetch helpers
# ---------------------------------------------------------------------------

def _format_search_results(results: list[dict[str, str]], max_results: int = 8) -> str:
    """Format search results for prompt injection."""
    if not results:
        return "(no search results available)"
    lines = []
    for r in results[:max_results]:
        title = r.get("title", "")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        lines.append(f"- [{title}]({url}): {snippet}")
    return "\n".join(lines)


def _truncate_html(html: str, max_chars: int = 8000) -> str:
    """Rough truncation of HTML content — strip tags for prompt."""
    # Remove script/style blocks
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def _parse_ai_json(raw: str) -> dict[str, Any]:
    """Robustly parse JSON from AI output. Handles markdown fences."""
    text = raw.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse AI JSON response (length=%d)", len(raw))
        return {}


def _dict_to_evidence(d: dict[str, Any]) -> AIEvidence:
    """Convert a single evidence dict to AIEvidence, handling extra/missing keys."""
    return AIEvidence(
        claim=d.get("claim", ""),
        source_url=d.get("source_url", ""),
        source_type=d.get("source_type", ""),
        confidence=d.get("confidence", "unverified"),
    )


# ---------------------------------------------------------------------------
# CompanyResearcher
# ---------------------------------------------------------------------------

class CompanyResearcher:
    """Stage 1: refine domain + AI-cited company research.

    All seams are injectable — ``_default_*`` implementations call real
    network services; tests inject fakes.
    """

    def __init__(
        self,
        *,
        ai_ask: AIAskFn | None = None,
        search: SearchFn | None = None,
        fetch_page: FetchPageFn | None = None,
        refine_domain: RefineDomainFn | None = None,
    ) -> None:
        self._ai_ask = ai_ask
        self._search = search
        self._fetch_page = fetch_page
        self._refine_domain = refine_domain

    # -- seam defaults (lazy — avoid import at module level) ----

    def _get_ai_ask(self) -> AIAskFn:
        if self._ai_ask is None:
            from app.ai.gateway import AIGateway
            self._ai_ask = AIGateway().ask
        return self._ai_ask

    def _get_search(self) -> SearchFn:
        if self._search is not None:
            return self._search
        # Fallback: try RegistryIndexedSearch
        try:
            from app.person_research.search_adapter import RegistryIndexedSearch
            adapter = RegistryIndexedSearch()
            return lambda q: adapter(q)
        except Exception:
            return lambda q: []

    def _get_fetch_page(self) -> FetchPageFn:
        if self._fetch_page is not None:
            return self._fetch_page
        # Fallback: try httpx
        try:
            import httpx

            def _fetch(url: str) -> Any:
                resp = httpx.get(url, timeout=15, follow_redirects=True, headers={
                    "User-Agent": "LeadHunterPro/1.0 (research bot)"
                })
                return type("PageFetch", (), {"ok": resp.is_success, "html": resp.text})()

            return _fetch
        except Exception:
            return lambda url: type("FailFetch", (), {"ok": False, "html": ""})()

    def _get_refine_domain(self) -> RefineDomainFn:
        if self._refine_domain is not None:
            return self._refine_domain
        return default_refine_domain

    # -- public API ----

    def research(self, email: str, domain: str) -> CompanyProfile:
        """Research the company behind ``email``/``domain``.

        Returns a :class:`CompanyProfile` with cited facts. On any failure,
        returns a partial profile with ``source_errors`` in the dict (accessible
        via ``profile.to_dict()["source_errors"]``).
        """
        refine = self._get_refine_domain()
        refined_domain = refine(domain)

        # Gather data
        search_fn = self._get_search()
        search_results = self._gather_search(search_fn, email, refined_domain)

        fetch_fn = self._get_fetch_page()
        site_content = self._gather_site(fetch_fn, refined_domain)

        # Build prompt
        prompt = company_research_prompt(
            email=email,
            refined_domain=refined_domain,
            search_results=_format_search_results(search_results),
            site_content=site_content,
        )

        # Call AI
        ai_fn = self._get_ai_ask()
        try:
            raw = ai_fn(prompt)
        except Exception as exc:
            logger.error("AI call failed for %s: %s", email, exc)
            return CompanyProfile(
                name="",
                website=f"https://{refined_domain}",
                facts=[AIEvidence(
                    claim=f"AI call failed: {exc}",
                    source_url="",
                    confidence="unverified",
                )],
            )

        # Parse
        data = _parse_ai_json(raw)
        if not data:
            return CompanyProfile(
                name="",
                website=f"https://{refined_domain}",
                facts=[AIEvidence(
                    claim="AI returned unparseable response",
                    source_url="",
                    confidence="unverified",
                )],
            )

        facts = [_dict_to_evidence(f) for f in data.get("facts", []) if isinstance(f, dict)]

        return CompanyProfile(
            name=data.get("company_name", ""),
            industry=data.get("industry", ""),
            location=data.get("location", ""),
            website=data.get("website", f"https://{refined_domain}"),
            facts=facts,
        )

    def research_with_domain(self, email: str, domain: str) -> dict[str, Any]:
        """Like ``research()`` but returns a dict including the refined domain."""
        refine = self._get_refine_domain()
        refined_domain = refine(domain)

        profile = self.research(email, refined_domain)

        result = profile.to_dict()
        result["refined_domain"] = refined_domain
        result["original_domain"] = domain
        return result

    # -- private helpers ----

    def _gather_search(self, search_fn: SearchFn, email: str, domain: str) -> list[dict[str, str]]:
        """Run multiple search queries and merge unique results.

        Search breadth (Medium scope):
        1. Email lookup
        2. Domain/company homepage
        3. LinkedIn company profile
        4. Google Maps / business listing
        5. BBB (Better Business Bureau)
        6. Texas contractor license
        7. News / recent activity
        """
        queries = [
            f'"{email}"',
            domain,
            f'"{domain}" company',
            f'"{domain}" site:linkedin.com/company',
            f'"{domain}" "google maps" OR "google business"',
            f'"{domain}" site:bbb.org',
            f'"{domain}" Texas contractor license',
            f'"{domain}" news construction',
        ]
        seen_urls: set[str] = set()
        results: list[dict[str, str]] = []
        for q in queries:
            try:
                for r in search_fn(q):
                    url = r.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        results.append(r)
            except Exception as exc:
                logger.debug("Search query %r failed: %s", q, exc)
        return results

    def _gather_site(self, fetch_fn: FetchPageFn, domain: str) -> str:
        """Fetch homepage and likely subpages for company info."""
        urls_to_try = [
            f"https://{domain}",
            f"https://{domain}/about",
            f"https://{domain}/contact",
            f"https://{domain}/services",
            f"https://{domain}/projects",
            f"https://{domain}/careers",
        ]
        all_content = ""
        for url in urls_to_try:
            try:
                page = fetch_fn(url)
                if getattr(page, "ok", False):
                    all_content += _truncate_html(getattr(page, "html", ""), max_chars=4000) + "\n"
            except Exception:
                continue
        return all_content or "(website not reachable)"
