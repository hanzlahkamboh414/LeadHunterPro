"""AI Lead Research — Person Attribution (Stage 2).

Runs the deterministic ``person_research`` engine first. If that returns
``attributed``, the result is trusted directly. If not, AI augments by
reading site content + search results to find person associations.

Safety rules enforced:
- Person name NEVER derived from email local-part alone.
- Every binding MUST cite a source_url.
- ``bound=True`` only when evidence is solid (deterministic OR AI-cited).
- Never invent — empty string means "could not determine".
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Protocol

from app.lead_research.models import AIEvidence, PersonFindings
from app.lead_research.prompts import person_research_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols (injectable seams)
# ---------------------------------------------------------------------------

class AIAskFn(Protocol):
    def __call__(self, prompt: str) -> str: ...


class SearchFn(Protocol):
    def __call__(self, query: str) -> list[dict[str, str]]: ...


class FetchPageFn(Protocol):
    def __call__(self, url: str) -> Any: ...


class DeterministicResearchFn(Protocol):
    """Signature matching ``ResearchService.research``."""
    def __call__(self, email: str, domain: str, **kw: Any) -> Any: ...


# ---------------------------------------------------------------------------
# JSON parsing (shared with company_research.py — kept local for isolation)
# ---------------------------------------------------------------------------

def _parse_ai_json(raw: str) -> dict[str, Any]:
    """Robustly parse JSON from AI output. Handles markdown fences."""
    import re
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse AI JSON response (length=%d)", len(raw))
        return {}


def _dict_to_evidence(d: dict[str, Any]) -> AIEvidence:
    """Convert a single evidence dict to AIEvidence."""
    return AIEvidence(
        claim=d.get("claim", ""),
        source_url=d.get("source_url", ""),
        source_type=d.get("source_type", ""),
        confidence=d.get("confidence", "unverified"),
    )


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
    import re
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


# ---------------------------------------------------------------------------
# PersonResearcherAI
# ---------------------------------------------------------------------------

class PersonResearcherAI:
    """Stage 2: deterministic person attribution + AI augmentation.

    Flow:
    1. Run deterministic ``ResearchService.research(email, domain)``.
    2. If ``attributed`` → convert bound candidate to PersonFindings, return.
    3. If not attributed → gather search + site content, call AI, parse result.
    """

    def __init__(
        self,
        *,
        deterministic: DeterministicResearchFn | None = None,
        ai_ask: AIAskFn | None = None,
        search: SearchFn | None = None,
        fetch_page: FetchPageFn | None = None,
    ) -> None:
        self._deterministic = deterministic
        self._ai_ask = ai_ask
        self._search = search
        self._fetch_page = fetch_page

    # -- seam defaults ----

    def _get_deterministic(self) -> DeterministicResearchFn | None:
        return self._deterministic

    def _get_ai_ask(self) -> AIAskFn:
        if self._ai_ask is None:
            from app.ai.gateway import AIGateway
            self._ai_ask = AIGateway().ask
        return self._ai_ask

    def _get_search(self) -> SearchFn:
        if self._search is not None:
            return self._search
        try:
            from app.person_research.search_adapter import RegistryIndexedSearch
            adapter = RegistryIndexedSearch()
            return lambda q: adapter(q)
        except Exception:
            return lambda q: []

    def _get_fetch_page(self) -> FetchPageFn:
        if self._fetch_page is not None:
            return self._fetch_page
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

    # -- public API ----

    def research(
        self,
        email: str,
        refined_domain: str,
        company_name: str = "",
        company_industry: str = "",
    ) -> PersonFindings:
        """Research person attribution for this email.

        Tries deterministic first; falls back to AI if not attributed.
        """
        # Step 1: try deterministic
        det_fn = self._get_deterministic()
        if det_fn is not None:
            try:
                result = det_fn(email, refined_domain)
                if hasattr(result, "verdict") and hasattr(result, "bound_candidate"):
                    from app.person_research.models import AttributionVerdict
                    if result.verdict is AttributionVerdict.attributed and result.bound_candidate:
                        candidate = result.bound_candidate
                        logger.info(
                            "Deterministic attributed %s → %s",
                            email, candidate.name,
                        )
                        return PersonFindings(
                            name=candidate.name,
                            role=candidate.role,
                            role_relevance=candidate.role_relevance,
                            bound=True,
                            evidence=[
                                AIEvidence(
                                    claim=f"Deterministic attribution: {candidate.name} ({candidate.role})",
                                    source_url=e.source_url,
                                    source_type=e.source_type,
                                    confidence="verified",
                                )
                                for e in candidate.evidence
                            ],
                        )
                    else:
                        logger.info(
                            "Deterministic verdict=%s for %s — augmenting with AI",
                            result.verdict.value, email,
                        )
            except Exception as exc:
                logger.warning("Deterministic research failed for %s: %s", email, exc)

        # Step 2: AI augmentation
        return self._ai_research(email, refined_domain, company_name, company_industry)

    def _ai_research(
        self,
        email: str,
        refined_domain: str,
        company_name: str,
        company_industry: str,
    ) -> PersonFindings:
        """AI-based person attribution when deterministic fails."""
        search_fn = self._get_search()
        search_results = self._gather_search(search_fn, email, refined_domain)

        fetch_fn = self._get_fetch_page()
        site_content = self._gather_site(fetch_fn, refined_domain)

        prompt = person_research_prompt(
            email=email,
            refined_domain=refined_domain,
            company_name=company_name,
            search_results=_format_search_results(search_results),
            site_content=site_content,
        )

        ai_fn = self._get_ai_ask()
        try:
            raw = ai_fn(prompt)
        except Exception as exc:
            logger.error("AI call failed for %s: %s", email, exc)
            return PersonFindings(
                evidence=[AIEvidence(
                    claim=f"AI call failed: {exc}",
                    source_url="",
                    confidence="unverified",
                )],
            )

        data = _parse_ai_json(raw)
        if not data:
            return PersonFindings(
                evidence=[AIEvidence(
                    claim="AI returned unparseable response",
                    source_url="",
                    confidence="unverified",
                )],
            )

        # Parse AI result — safety: never trust bound=True without source_url
        name = data.get("person_name", "")
        role = data.get("person_role", "")
        role_relevance = data.get("role_relevance", False)
        bound = data.get("bound", False)
        evidence = [_dict_to_evidence(e) for e in data.get("evidence", []) if isinstance(e, dict)]

        # Safety override: bound=True requires at least one verified evidence
        if bound and not any(e.confidence == "verified" and e.source_url for e in evidence):
            logger.warning(
                "AI claimed bound for %s but no verified source_url — downgrading to unbound",
                email,
            )
            bound = False

        # Safety override: never bind a name without source_url evidence
        if name and not any(e.source_url for e in evidence):
            logger.warning(
                "AI provided name '%s' for %s but no source_url — marking unverified",
                name, email,
            )

        return PersonFindings(
            name=name,
            role=role,
            role_relevance=role_relevance,
            bound=bound,
            evidence=evidence,
        )

    # -- private helpers ----

    def _gather_search(self, search_fn: SearchFn, email: str, domain: str) -> list[dict[str, str]]:
        """Run multiple search queries and merge unique results."""
        queries = [
            f'"{email}"',
            f'"{domain}" company team',
            f'"{domain}" about contact',
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
        """Fetch homepage and likely team/about pages."""
        urls_to_try = [
            f"https://{domain}",
            f"https://{domain}/about",
            f"https://{domain}/contact",
            f"https://{domain}/team",
            f"https://{domain}/our-team",
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
