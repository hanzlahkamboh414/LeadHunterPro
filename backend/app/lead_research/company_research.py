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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Protocol

from app.lead_research.models import AIEvidence, CompanyProfile
from app.lead_research.prompts import company_research_prompt, deep_research_prompt
from app.lead_research.provenance import (
    find_linkedin_company_url,
    guard_evidence_location,
    labeled_page_block,
    linkedin_block,
    linkedin_lane_enabled,
)

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

    ``mx_check`` defaults to the fast native resolver ``domain_has_mx_fast``
    (dnspython over public IPv4 DNS) — the HTTPS-DoH ``domain_has_mx`` is
    correct but ~20s per lookup on some networks, which would dominate per-lead
    latency.
    """
    if mx_check is None:
        from app.email.domain_verifier import domain_has_mx_fast
        mx_check = domain_has_mx_fast

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


def domain_delivers_email(
    domain: str,
    *,
    mx_check: Callable[[str], bool] | None = None,
) -> bool:
    """True when *domain* actually resolves MX — the address can receive mail.

    Reuses :func:`default_refine_domain` so TLD corrections are applied first
    (e.g. ``acme.comm`` → ``acme.com``); the final verdict is whether the
    refined domain has a resolvable MX record. A domain with no MX cannot
    receive email, so a lead on it is undeliverable (dead/expired) — this is
    the hard gate that keeps such leads out of ``contact_now``.
    """
    if mx_check is None:
        from app.email.domain_verifier import domain_has_mx_fast
        mx_check = domain_has_mx_fast

    refined = default_refine_domain(domain, mx_check=mx_check)
    try:
        return mx_check(refined)
    except Exception:
        logger.debug("MX check failed for refined domain %s", refined)
        return False


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
# Concurrency helpers
# ---------------------------------------------------------------------------

#: Cap on parallel search workers. Real search providers (Tavily) rate-limit,
#: and each worker spins its own event loop, so we parallelize the big win
#: (sequential -> one round) without hammering the provider with unbounded
#: threads. 5 stays well under typical per-minute limits while collapsing
#: 5-6 sequential queries into ~1 round-trip.
_MAX_SEARCH_WORKERS = 5


def _search_workers(n: int) -> int:
    """Bounded worker count for a batch of ``n`` queries."""
    if n <= 1:
        return 1
    return min(_MAX_SEARCH_WORKERS, n)


# ---------------------------------------------------------------------------
# Contractor-licence region
#
# Licence registries are per-state, so a licence query must name the state the
# company is actually in. The deep query used to hardcode "Texas" while only
# 6.5% of live dossiers are TX (FL 37, TX 33, CT 19, IA 12, WI 11 of 505), so
# it queried the wrong registry for ~93% of leads.
#
# Full names are mapped as well as codes because the researched location field
# arrives in both shapes ("Houston, Texas" and "Dallas, TX" both occur live).
# ---------------------------------------------------------------------------
_STATE_NAMES: dict[str, str] = {
    "alabama": "Alabama", "alaska": "Alaska", "arizona": "Arizona",
    "arkansas": "Arkansas", "california": "California", "colorado": "Colorado",
    "connecticut": "Connecticut", "delaware": "Delaware", "florida": "Florida",
    "georgia": "Georgia", "hawaii": "Hawaii", "idaho": "Idaho",
    "illinois": "Illinois", "indiana": "Indiana", "iowa": "Iowa",
    "kansas": "Kansas", "kentucky": "Kentucky", "louisiana": "Louisiana",
    "maine": "Maine", "maryland": "Maryland", "massachusetts": "Massachusetts",
    "michigan": "Michigan", "minnesota": "Minnesota", "mississippi": "Mississippi",
    "missouri": "Missouri", "montana": "Montana", "nebraska": "Nebraska",
    "nevada": "Nevada", "new hampshire": "New Hampshire",
    "new jersey": "New Jersey", "new mexico": "New Mexico", "new york": "New York",
    "north carolina": "North Carolina", "north dakota": "North Dakota",
    "ohio": "Ohio", "oklahoma": "Oklahoma", "oregon": "Oregon",
    "pennsylvania": "Pennsylvania", "rhode island": "Rhode Island",
    "south carolina": "South Carolina", "south dakota": "South Dakota",
    "tennessee": "Tennessee", "texas": "Texas", "utah": "Utah",
    "vermont": "Vermont", "virginia": "Virginia", "washington": "Washington",
    "west virginia": "West Virginia", "wisconsin": "Wisconsin",
    "wyoming": "Wyoming", "district of columbia": "District of Columbia",
}

_STATE_CODES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def _license_region(location: str) -> str:
    """US state named in ``location``, or ``""`` when none can be read.

    Returning ``""`` matters: the caller then asks a state-neutral licence
    question instead of naming a state the evidence does not support
    (CLAUDE.md §12 — nothing invented).
    """
    text = (location or "").strip()
    if not text:
        return ""
    low = text.lower()
    # Full state name anywhere in the string ("Houston, Texas", "Texas").
    # Longest first so "West Virginia" is not shadowed by "Virginia".
    for name in sorted(_STATE_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", low):
            return _STATE_NAMES[name]
    # Two-letter code as its own token ("Dallas, TX"). Case-sensitive: a
    # lowercase "in"/"or"/"me" is an English word, not a state.
    for token in re.findall(r"\b[A-Z]{2}\b", text):
        state = _STATE_CODES.get(token)
        if state:
            return state
    return ""


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
        source_note=d.get("source_note", ""),
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
        linkedin_extract: Callable[[str], str] | None = None,
    ) -> None:
        self._ai_ask = ai_ask
        self._search = search
        self._fetch_page = fetch_page
        self._refine_domain = refine_domain
        self._linkedin_extract = linkedin_extract

    # -- seam defaults (lazy — avoid import at module level) ----

    def _get_ai_ask(self) -> AIAskFn:
        if self._ai_ask is None:
            from app.ai.gateway import AIGateway
            self._ai_ask = AIGateway().ask
        return self._ai_ask

    def _get_deep_ai_ask(self) -> AIAskFn:
        """AI callable for the deep-research stage, on the second key.

        Uses ``AI_API_KEY_2`` (falling back to the primary key) so deep
        research runs on its own rate limit, parallel to the main pipeline.
        When a test injects ``ai_ask``, that fake is reused (no network).
        """
        if self._ai_ask is not None:
            return self._ai_ask
        from app.ai.gateway import make_ai_ask
        return make_ai_ask()

    def _get_search(self) -> SearchFn:
        if self._search is not None:
            return self._search
        # Fallback: try RegistryIndexedSearch (callable AND exposes ``search_many``
        # for the concurrent path used by _gather_search).
        try:
            from app.person_research.search_adapter import RegistryIndexedSearch
            return RegistryIndexedSearch()
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

    def _get_linkedin_extract(self) -> Callable[[str], str]:
        """Extract-callable for the LinkedIn lane (Tavily ``/extract`` via the
        registry adapter — replaceable, never a hardcoded connector).

        Falls back to returning "" for every URL when no enabled provider
        exposes extraction, so a missing capability is a silent no-op (the
        lane is additive — CLAUDE.md §4/§12).
        """
        if self._linkedin_extract is not None:
            return self._linkedin_extract
        try:
            from app.person_research.search_adapter import RegistryIndexedSearch
            adapter = RegistryIndexedSearch()
            if getattr(adapter, "extract_one", None) is not None:
                return lambda url: (adapter.extract_one(url) or "")
        except Exception:
            logger.debug("LinkedIn extract seam unavailable", exc_info=True)
        return lambda url: ""

    # -- public API ----

    def research(
        self, email: str, domain: str, *, trade: str = "", location: str = "",
    ) -> CompanyProfile:
        """Research the company behind ``email``/``domain``.

        ``trade``/``location`` (optional) carry the known construction context
        from the discovery layer, so the AI verifies the company as a
        construction-bid contractor rather than drifting to generic results.

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

        # LinkedIn company activity lane (additive, config-gated): the public
        # LinkedIn company page surfaced in search results is extracted +
        # labeled exactly like a website page, so the AI can cite its activity/
        # news/hiring (thence source_type "linkedin"). A login-walled page
        # contributes nothing — never fabricated (CLAUDE.md §12).
        if linkedin_lane_enabled() and search_results:
            li_url = find_linkedin_company_url(search_results)
            if li_url:
                li_block = linkedin_block(li_url, self._get_linkedin_extract(), max_chars=4000)
                if li_block:
                    site_content += li_block

        # Build prompt
        prompt = company_research_prompt(
            email=email,
            refined_domain=refined_domain,
            search_results=_format_search_results(search_results),
            site_content=site_content,
            trade=trade,
            location=location,
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

        # Identity fabrication guard (CLAUDE.md §12 — never return synthetic
        # companies as production results): if the site itself was unreadable
        # AND the AI returned no source-bearing fact that would let it actually
        # see the company (no cited URL anywhere in the evidence), then a
        # name/industry/location is at best the model inventing identity from
        # nothing. Wipe the identity block so the company reads "unknown" and
        # can never be scored as a real contractor. This is the exact failure
        # behind nsarro@unitedcr.com: an unreachable site + fabricated
        # "Insurance Restoration Contractor" that passed the scorer's keyword.
        site_unreadable = (
            not site_content or "(website not reachable)" in site_content
        )
        has_cited_fact = any(f.source_url for f in facts)
        if site_unreadable and not has_cited_fact:
            logger.info(
                "Identity guard %s: site unreadable and no cited fact — wiping company identity",
                email,
            )
            data["company_name"] = ""
            data["industry"] = ""
            data["location"] = ""

        # Exact-page guard (enforcement behind the provenance rule): a
        # "verified" claim whose source is a bare domain root with no exact
        # location reported is demoted to "unverified" with the honest reason
        # in source_note — a claim the model cannot place on a page must not
        # claim verification.
        facts = guard_evidence_location(facts)

        return CompanyProfile(
            name=data.get("company_name", ""),
            industry=data.get("industry", ""),
            location=data.get("location", ""),
            website=data.get("website", f"https://{refined_domain}"),
            facts=facts,
        )

    def research_with_domain(
        self, email: str, domain: str, *, trade: str = "", location: str = "",
    ) -> dict[str, Any]:
        """Like ``research()`` but returns a dict including the refined domain."""
        refine = self._get_refine_domain()
        refined_domain = refine(domain)

        profile = self.research(email, refined_domain, trade=trade, location=location)

        result = profile.to_dict()
        result["refined_domain"] = refined_domain
        result["original_domain"] = domain
        return result

    def research_deep(
        self, domain: str, company_name: str, *, location: str = ""
    ) -> list[AIEvidence]:
        """Stage 1b — deep-dive growth/need signals for a qualifying lead.

        Runs the 5 growth queries (licence, news, hiring, expansion, bid-win)
        and returns an AI-cited list of growth facts. Returns ``[]`` on any
        failure (graceful — deep dive is additive, never fatal).

        ``location`` is the company location already researched in Stage 1. It
        aims the contractor-licence query at the RIGHT state registry; without
        it the query stays state-neutral rather than guessing (see
        :func:`_license_region`).
        """
        search_fn = self._get_search()
        search_results = self._gather_search(
            search_fn, "", domain, queries=self._deep_queries(domain, location)
        )

        prompt = deep_research_prompt(
            domain=domain,
            company_name=company_name,
            search_results=_format_search_results(search_results),
        )

        # Deep research runs on its own lane (AI_API_KEY_2) so it does not
        # share a rate limit with the main pipeline.
        ai_fn = self._get_deep_ai_ask()
        try:
            raw = ai_fn(prompt)
        except Exception as exc:
            logger.error("Deep research AI call failed for %s: %s", domain, exc)
            return []

        data = _parse_ai_json(raw)
        if not data:
            return []

        facts = [_dict_to_evidence(f) for f in data.get("facts", []) if isinstance(f, dict)]
        return guard_evidence_location(facts)

    # -- private helpers ----

    def _screening_queries(self, email: str, domain: str) -> list[str]:
        """Fast screening queries — identity + verification (used on every lead).

        Queries are only emitted when their anchor term is present. An empty
        ``domain`` would otherwise produce a bare ``site:`` query (e.g.
        ``"" site:linkedin.com/company``), which Tavily rejects with HTTP 400
        "Query cannot consist only of site: operators" — the empty quoted
        string is stripped, leaving only the site: operator.

        Query set is MEASURED, not guessed. Across 506 live dossiers the
        citation yield per target was:

            site:linkedin.com/company   105 dossiers (20.9%)  <- kept
            site:bbb.org                  5 dossiers ( 1.0%)  <- dropped

        The BBB dork spent a credit on every single lead to be cited by one in
        a hundred, and every fact it ever produced (company name, phone,
        address) is also carried by the LinkedIn company page and the site
        crawl. Dropping it removes a paid call without removing a fact — the
        only kind of reduction that does not cost quality (CLAUDE.md §11).
        """
        queries: list[str] = []
        if (email or "").strip():
            queries.append(f'"{email}"')
        if (domain or "").strip():
            queries.extend([
                domain,
                f'"{domain}" company',
                f'"{domain}" site:linkedin.com/company',
            ])
        return queries

    def _deep_queries(self, domain: str, location: str = "") -> list[str]:
        """Deep-dive queries — growth/need signals (only for qualifying leads).

        Two measured corrections over the previous 6-query set:

        1. **The maps query is gone.** ``"google maps" OR "google business"``
           was cited by **0 of 506** dossiers — a guaranteed-waste credit on
           every deep lead. Its intent (does this company physically exist)
           is already served by the site crawl and the LinkedIn company page.

        2. **The licence query is no longer hardcoded to Texas.** Only 33 of
           505 dossiers (6.5%) are TX; the live mix is FL 37, TX 33, CT 19,
           IA 12, WI 11. So ``"{domain}" Texas contractor license`` was asking
           the wrong state's registry for ~93% of leads — a correctness bug,
           not just a wasted credit. The state now comes from the researched
           company location, and with no known location the query falls back
           to a state-neutral ``contractor license`` form rather than naming a
           state we have no evidence for (CLAUDE.md §12 — never invent a fact).

        The four surviving signal queries are the ones the evidence actually
        cites: bid/award (272 keyword hits in intent evidence), hiring (131),
        estimator (103), expansion (101).
        """
        state = _license_region(location)
        license_query = (
            f'"{domain}" {state} contractor license'
            if state
            else f'"{domain}" contractor license'
        )
        return [
            license_query,
            f'"{domain}" news construction',
            f'"{domain}" hiring estimator OR "cost estimator" OR "project manager"',
            f'"{domain}" "new office" OR expansion OR "opening location"',
            f'"{domain}" "awarded" OR "low bidder" OR "bid award" OR "contract award"',
        ]

    def _gather_search(
        self,
        search_fn: SearchFn,
        email: str,
        domain: str,
        queries: list[str] | None = None,
    ) -> list[dict[str, str]]:
        """Run multiple search queries (CONCURRENTLY) and merge unique results.

        Two-stage search breadth:
        - Screening (5 queries): email, domain, company, LinkedIn, BBB
        - Deep (6 queries, via ``research_deep``): maps, license, news,
          hiring, expansion, bid-win

        The queries are independent network calls, so they run in a thread
        pool instead of one-after-another. Each search adapter call spins its
        own event loop (see search_adapter), so threads are safe; this is the
        biggest single reduction in per-lead latency (11-16 queries -> ~1
        round). ``seen_urls`` is only touched by the collecting thread, so
        dedup stays race-free.
        """
        if queries is None:
            queries = self._screening_queries(email, domain)
        seen_urls: set[str] = set()
        results: list[dict[str, str]] = []

        # Preferred path: the real adapter runs all queries concurrently in ONE
        # event loop (thread-safe — providers hold a shared aiohttp session that
        # must stay in one loop). Fall back to a threaded fan-out for injected
        # plain-callable seams (test fakes, no aiohttp).
        many = getattr(search_fn, "search_many", None)
        if many is not None:
            batches = many(list(queries))
            for batch in batches:
                for r in batch:
                    url = r.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        results.append(r)
            return results

        def _one(q: str) -> list[dict[str, str]]:
            try:
                return list(search_fn(q))
            except Exception as exc:
                logger.debug("Search query %r failed: %s", q, exc)
                return []

        with ThreadPoolExecutor(max_workers=_search_workers(len(queries))) as ex:
            futures = [ex.submit(_one, q) for q in queries]
            for fut in as_completed(futures):
                for r in fut.result():
                    url = r.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        results.append(r)
        return results

    def _gather_site(self, fetch_fn: FetchPageFn, domain: str) -> str:
        """Fetch homepage and likely subpages for company info (CONCURRENTLY).

        Every successfully fetched page becomes a LABELED block —
        ``[PAGE: About us — https://acme.com/about]`` + text — so the AI can
        cite the EXACT page each fact came from. This is the root fix for the
        user's evidence complaint: the previous anonymous concatenation made it
        impossible for the model to know which block was ``/about``, so the
        only URL it could honestly cite was the bare domain root.
        """
        urls_to_try = [
            f"https://{domain}",
            f"https://{domain}/about",
            f"https://{domain}/contact",
            f"https://{domain}/services",
            f"https://{domain}/projects",
            f"https://{domain}/careers",
        ]

        def _one(url: str) -> str:
            try:
                page = fetch_fn(url)
                if getattr(page, "ok", False):
                    return labeled_page_block(url, getattr(page, "html", ""), max_chars=4000)
            except Exception:
                pass
            return ""

        chunks: list[str] = []
        with ThreadPoolExecutor(max_workers=min(6, len(urls_to_try))) as ex:
            futures = [ex.submit(_one, u) for u in urls_to_try]
            for fut in as_completed(futures):
                chunk = fut.result()
                if chunk:
                    chunks.append(chunk)
        return "".join(chunks) or "(website not reachable)"
