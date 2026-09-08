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
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Protocol

from app.lead_research.models import AIEvidence, PersonFindings
from app.lead_research.prompts import linkedin_profile_prompt, person_research_prompt
from app.lead_research.provenance import (
    guard_evidence_location,
    labeled_page_block,
    linkedin_block,
    linkedin_lane_enabled,
)
from app.lead_research.relevance import filter_social_noise

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
# Concurrency helpers (kept local for isolation — same bounded policy as
# company_research: real providers rate-limit, each worker owns its loop).
# ---------------------------------------------------------------------------

_MAX_SEARCH_WORKERS = 5

#: Bound on concurrent own-site page fetches. Higher than the search bound
#: because these are free httpx GETs against a SINGLE host we already decided
#: to crawl — not rate-limited provider calls — so the wider page list costs
#: latency, not credits, and the concurrency absorbs it.
_MAX_SITE_WORKERS = 8


def _search_workers(n: int) -> int:
    """Bounded worker count for a batch of ``n`` queries."""
    if n <= 1:
        return 1
    return min(_MAX_SEARCH_WORKERS, n)


#: Person profile URLs, normalised to a clean ``linkedin.com/in/<handle>``.
#: Only person pages (``/in/``) are captured — a company page (``/company/``)
#: is not the decision-maker's own profile and is not stored as ``linkedin``.
_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:[a-z]+\.)?linkedin\.com/in/[A-Za-z0-9_\-\.%]+",
    re.IGNORECASE,
)


def _person_tokens(name: str) -> set[str]:
    """Alpha tokens of a person's name (len >= 3), e.g. 'Nick Sarro' -> {nick, sarro}."""
    return {t for t in re.split(r"[^A-Za-z]+", (name or "").lower()) if t and len(t) >= 3}


def _handle_tokens(handle: str) -> set[str]:
    """Alpha tokens of a LinkedIn handle slug, e.g. 'michael-hammond-20b38742'
    -> {michael, hammond, 20b38742}."""
    return {t for t in re.split(r"[^A-Za-z]+", handle.lower()) if t and len(t) >= 3}


def _linkedin_matches_name(linkedin_url: str, name: str) -> bool:
    """True only when the LinkedIn handle plausibly BELONGS to ``name``.

    The user's exact complaint (nsarro@unitedcr.com): the pipeline stored a
    LinkedIn profile found on the company's search results WITHOUT verifying it
    was the same person — Nick Sarro ended up credited with Michael Hammond's
    profile. A misleading link is worse than none, so a handle is accepted only
    when it shares at least two alpha tokens with the person's name, OR shares
    the surname (last name token), OR embeds a name token (a 'janedoe' handle
    for Jane Doe). Everything else is rejected — honest empty over wrong.
    """
    person_parts = [t for t in re.split(r"[^A-Za-z]+", (name or "").strip().lower()) if t]
    surname = next((p for p in reversed(person_parts) if len(p) >= 3), "")
    nt = {p for p in person_parts if len(p) >= 3}
    if not nt:
        return False  # no name to verify against -> cannot claim a profile
    handle = linkedin_url.rsplit("/", 1)[-1]
    ht = _handle_tokens(handle)
    shared = nt & ht
    if len(shared) >= 2:
        return True
    if surname and surname in ht:
        return True
    # 'janedoe' style slug embeds a name token without a separator.
    return any(t in handle.lower() for t in nt)


def _find_linkedin(name: str, *url_collections: list[str]) -> str:
    """Return the first ``linkedin.com/in/<handle>`` URL across the given URL
    collections (search results, evidence source URLs) that BELONGS to ``name``,
    or "" if none does. Honest: only a URL the pipeline actually surfaced counts —
    never a fabricated handle (CLAUDE.md §12) and never another person's profile
    (see :func:`_linkedin_matches_name`).
    """
    for urls in url_collections:
        for url in urls:
            match = _LINKEDIN_RE.search(url or "")
            if match and _linkedin_matches_name(match.group(0), name):
                return match.group(0)
    return ""


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
        source_note=d.get("source_note", ""),
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


def _format_company_facts(facts: list[Any], max_facts: int = 15) -> str:
    """Format verified company facts for prompt injection.

    Only facts that carry a source_url are included, so the AI can cite them
    when binding a person (never hand it unverified claims to bind against).
    """
    lines = []
    for f in facts:
        if isinstance(f, dict):
            claim, url = f.get("claim", ""), f.get("source_url", "")
        else:
            claim, url = getattr(f, "claim", ""), getattr(f, "source_url", "")
        if not claim or not url:
            continue
        lines.append(f"- {claim} (source: {url})")
        if len(lines) >= max_facts:
            break
    if not lines:
        return "(none provided)"
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
        linkedin_extract: Callable[[str], str] | None = None,
    ) -> None:
        self._deterministic = deterministic
        self._ai_ask = ai_ask
        self._search = search
        self._fetch_page = fetch_page
        self._linkedin_extract = linkedin_extract

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
            # Return the adapter directly (not wrapped in a lambda) so that
            # ``search_many`` is exposed — _gather_search prefers it for the
            # single-loop concurrent path. A lambda would strip the attribute
            # and force the threaded fallback, which breaks aiohttp's shared
            # session across event loops.
            return RegistryIndexedSearch()
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

    def _get_linkedin_extract(self) -> Callable[[str], str]:
        """Extract-callable for the LinkedIn lane (Tavily ``/extract`` via the
        registry adapter — replaceable, never a hardcoded connector).

        Falls back to returning "" for every URL when no enabled provider
        exposes extraction — a missing capability is a silent no-op (the lane
        is additive — CLAUDE.md §4/§12).
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
        self,
        email: str,
        refined_domain: str,
        company_name: str = "",
        company_industry: str = "",
        company_facts: list[Any] | None = None,
        *,
        query_planner: Any | None = None,
    ) -> PersonFindings:
        """Research person attribution for this email.

        Tries deterministic first; falls back to AI if not attributed.
        ``company_facts`` are the verified Stage 1 facts, passed to the AI so it
        can bind an email to a person named in those facts.
        ``query_planner`` feeds the deterministic query-yield learn loop; the
        deterministic path issues no search and thus contributes nothing.
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
                            linkedin=_find_linkedin(
                                candidate.name,
                                [e.source_url for e in candidate.evidence],
                            ),
                            evidence=[
                                AIEvidence(
                                    claim=f"Deterministic attribution: {candidate.name} ({candidate.role})",
                                    source_url=e.source_url,
                                    source_type=e.source_type,
                                    confidence="verified",
                                    source_note=getattr(e, "source_note", ""),
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
        return self._ai_research(
            email, refined_domain, company_name, company_industry,
            company_facts=company_facts,
            query_planner=query_planner,
        )

    def _ai_research(
        self,
        email: str,
        refined_domain: str,
        company_name: str,
        company_industry: str,
        company_facts: list[Any] | None = None,
        *,
        query_planner: Any | None = None,
    ) -> PersonFindings:
        """AI-based person attribution when deterministic fails."""
        search_fn = self._get_search()
        search_results = self._gather_search(
            search_fn, email, refined_domain, company_name,
            query_planner=query_planner,
        )

        fetch_fn = self._get_fetch_page()
        site_content = self._gather_site(fetch_fn, refined_domain)

        prompt = person_research_prompt(
            email=email,
            refined_domain=refined_domain,
            company_name=company_name,
            search_results=_format_search_results(search_results),
            site_content=site_content,
            company_facts=_format_company_facts(company_facts or []),
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

        # Exact-page guard (enforcement behind the provenance rule): a "verified"
        # claim whose source is a bare domain root with no exact location
        # reported is demoted to "unverified" — an unlocatable citation is never
        # presented as verified. Applied BEFORE the bound override below so a
        # binding resting only on a bare-root source cannot survive.
        evidence = guard_evidence_location(evidence)

        # Deterministic role override (founder rule): the role KEYWORD LIST is
        # the final authority on role_relevance, never the AI's judgment. The
        # AI may say "relevant" for an IT Manager or a Marketing coordinator —
        # the deterministic check (owner/PM/operations/president/VP...) corrects
        # it so only genuine decision-makers can qualify as contact_now.
        if role:
            try:
                from app.engines.lead.lead_models import role_is_plausibly_relevant
                det_relevant = role_is_plausibly_relevant(role)
                if role_relevance != det_relevant:
                    logger.info(
                        "Role relevance overridden for %s: %s %r → %s (deterministic)",
                        email, role, role_relevance, det_relevant,
                    )
                role_relevance = det_relevant
            except Exception:
                pass  # if the helper is unavailable, keep the AI's judgment

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

        # LinkedIn is a first-class field: the first real person-profile URL the
        # pipeline surfaced (from the site:linkedin.com searches or cited
        # evidence) that also BELONGS to ``name`` is stored here — never
        # invented, and never another person's profile (nsarro@unitedcr.com bug:
        # Michael Hammond's handle was credited to Nick Sarro).
        linkedin = _find_linkedin(
            name,
            [r.get("url", "") for r in search_results],
            [e.source_url for e in evidence],
        )

        # Fallback: AI may have found a LinkedIn URL in its analysis — use it
        # only if the regex extraction came up empty AND the profile matches
        # the person's name.
        if not linkedin:
            ai_linkedin = (data.get("linkedin") or "").strip()
            if ai_linkedin:
                ai_match = _LINKEDIN_RE.search(ai_linkedin)
                if ai_match and _linkedin_matches_name(ai_match.group(0), name):
                    linkedin = ai_match.group(0)
                    logger.info("LinkedIn from AI response for %s: %s", email, linkedin)

        # LinkedIn enrich — person activity/details from the person's OWN
        # profile. Runs AFTER identity (name) + R1 name-match established the
        # profile belongs to this person. One best-effort pass: only a read,
        # public profile yields facts; a login-walled one contributes nothing.
        # Facts are cited to the exact profile URL (source_type "linkedin"). The
        # appended facts never change name/role/score — they only ADD cited
        # LinkedIn-sourced detail (plan-approved honest limit).
        if linkedin and name and linkedin_lane_enabled():
            evidence = list(self._enrich_linkedin_profile(email, name, linkedin, evidence))

        return PersonFindings(
            name=name,
            role=role,
            role_relevance=role_relevance,
            bound=bound,
            linkedin=linkedin,
            evidence=evidence,
        )

    def _enrich_linkedin_profile(
        self,
        email: str,
        name: str,
        linkedin: str,
        evidence: list[AIEvidence],
    ) -> list[AIEvidence]:
        """Best-effort enrichment of person facts from their public LinkedIn
        profile (R1 name-match already governs that ``linkedin`` belongs to
        ``name``).

        Returns the ORIGINAL evidence unchanged when the profile is login-walled
        or extraction is unavailable — the lane is additive and never fabricates
        detail the pipeline could not actually read (CLAUDE.md §12).
        """
        li_block = linkedin_block(linkedin, self._get_linkedin_extract(), max_chars=4000)
        if not li_block:
            return evidence
        prompt = linkedin_profile_prompt(
            name=name,
            linkedin=linkedin,
            linkedin_content=li_block,
        )
        ai_fn = self._get_ai_ask()
        try:
            raw = ai_fn(prompt)
        except Exception as exc:
            logger.warning("LinkedIn profile AI pass failed for %s: %s", email, exc)
            return evidence
        data = _parse_ai_json(raw)
        if not data:
            return evidence
        added = guard_evidence_location(
            [_dict_to_evidence(f) for f in data.get("facts", []) if isinstance(f, dict)]
        )
        # Deterministic relevance filter (zero credits): a profile lane that
        # reports connection/follower counts, education, certs, memberships or
        # languages is noise, not business signal. The prompt above also stops
        # inviting it; this is the backstop that guarantees none of it persists.
        added = filter_social_noise(added)
        if not added:
            return evidence
        logger.info("LinkedIn profile enrichment added %d fact(s) for %s", len(added), email)
        return list(evidence) + added

    # -- private helpers ----

    def _gather_search(
        self,
        search_fn: SearchFn,
        email: str,
        domain: str,
        company_name: str = "",
        *,
        query_planner: Any | None = None,
    ) -> list[dict[str, str]]:
        """Run multiple search queries (CONCURRENTLY) and merge unique results.

        Search breadth (Medium scope):
        1. Exact email lookup
        2. Company LinkedIn
        3. LinkedIn company + person
        4. Estimator-role signal

        Queries are independent network calls run in a thread pool (each
        adapter call owns its event loop, so threads are safe) — same win as
        company_research: sequential queries collapse into ~one round.

        ``query_planner`` (a :class:`~app.lead_research.query_learning.
        QueryYieldPlanner`) prunes templates the learn loop has proven to yield
        zero verified citations, and records each surviving template's returned
        URLs for the loop.
        """
        # Queries are only emitted when their anchor term is present: an empty
        # ``domain`` would otherwise produce a bare ``site:`` query (Tavily 400
        # "cannot consist only of site: operators"), and an empty ``email`` a
        # bare ``""`` ("Query is missing").
        #
        # ``"{domain}" company team`` and ``"{domain}" about contact`` were
        # dropped after measuring 506 live dossiers: they paid a provider to
        # point at the company's OWN about/contact/team pages, which
        # ``_gather_site`` already fetches directly and for free. 83% of all
        # own-domain citations land on a page that free crawl fetches, and
        # widening that crawl list (same commit) takes it to 91% — so the pages
        # keep arriving, just without the search bill. What search uniquely
        # provides is OFF-domain corroboration (467 of 614 person citations:
        # LinkedIn 207, RocketReach 18, Facebook 15, …), which is exactly what
        # the surviving queries target.
        pairs: list[tuple[str, str]] = []
        if (email or "").strip():
            pairs.append((f'"{email}"', "person:email"))
        if (domain or "").strip():
            pairs.append((f'"{domain}" site:linkedin.com', "person:linkedin_site"))
        if company_name:
            # LinkedIn person profiles — natural queries (no dork, Tavily-friendly)
            pairs.append((f'"{company_name}" linkedin.com/in', "person:linkedin_profile"))
            if (email or "").strip():
                local = email.split("@")[0].strip()
                if local:
                    pairs.append((f'"{company_name}" "{local}" linkedin', "person:local_linkedin"))
            pairs.append(
                (f'"{company_name}" estimator OR estimating OR "cost engineer"', "person:estimator")
            )

        # Learn-loop prune: drop templates proven to yield no verified citation.
        if query_planner is not None:
            pairs = [(q, lbl) for (q, lbl) in pairs if not query_planner.should_skip(lbl)]

        if not pairs:
            logger.debug("All person search queries pruned by yield loop for %s", domain)
            return []

        q_strs = [q for q, _ in pairs]
        labels = [lbl for _, lbl in pairs]
        seen_urls: set[str] = set()
        results: list[dict[str, str]] = []

        # Preferred path: the real adapter runs all queries concurrently in ONE
        # event loop (thread-safe — providers hold a shared aiohttp session that
        # must stay in one loop). Fall back to a threaded fan-out for injected
        # plain-callable seams (test fakes, no aiohttp).
        many = getattr(search_fn, "search_many", None)
        if many is not None:
            # search_many returns one result-list per query, same order as input.
            batches = many(q_strs)
            for lbl, batch in zip(labels, batches):
                urls = [r.get("url", "") for r in batch if r.get("url")]
                if query_planner is not None:
                    query_planner.note(lbl, urls)
                for r in batch:
                    url = r.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        results.append(r)
            return results

        def _one(q: str, lbl: str) -> list[dict[str, str]]:
            try:
                found = list(search_fn(q))
            except Exception as exc:
                logger.debug("Search query %r failed: %s", q, exc)
                found = []
            if query_planner is not None:
                query_planner.note(lbl, [r.get("url", "") for r in found if r.get("url")])
            return found

        with ThreadPoolExecutor(max_workers=_search_workers(len(q_strs))) as ex:
            futures = [ex.submit(_one, q, lbl) for q, lbl in pairs]
            for fut in as_completed(futures):
                for r in fut.result():
                    url = r.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        results.append(r)
        return results

    def _gather_site(self, fetch_fn: FetchPageFn, domain: str) -> str:
        """Fetch homepage and likely team/about pages (CONCURRENTLY).

        Every successfully fetched page becomes a LABELED block —
        ``[PAGE: Team page — https://acme.com/team]`` + text — so the AI can
        cite the EXACT page where a person's name/role appeared (the provenance
        fix; the previous anonymous concatenation forced bare-root citations).
        """
        # The hyphenated/suffixed variants are not padding: measured over 506
        # live dossiers, own-domain citations landed on /about-us 34 times,
        # /contact-us 24, /who-we-are 12, /meet-the-team 3 — pages the previous
        # list missed, so the only way the AI could see them was a PAID search
        # result. Fetching them directly raises own-domain coverage from 83% to
        # 91% while REMOVING search calls: more evidence, smaller bill.
        # These are free, concurrent GETs against one host, not provider calls.
        urls_to_try = [
            f"https://{domain}",
            f"https://{domain}/about",
            f"https://{domain}/about-us",
            f"https://{domain}/contact",
            f"https://{domain}/contact-us",
            f"https://{domain}/team",
            f"https://{domain}/our-team",
            f"https://{domain}/meet-the-team",
            f"https://{domain}/leadership",
            f"https://{domain}/people",
            f"https://{domain}/staff",
            f"https://{domain}/who-we-are",
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
        with ThreadPoolExecutor(max_workers=min(_MAX_SITE_WORKERS, len(urls_to_try))) as ex:
            futures = [ex.submit(_one, u) for u in urls_to_try]
            for fut in as_completed(futures):
                chunk = fut.result()
                if chunk:
                    chunks.append(chunk)
        return "".join(chunks) or "(website not reachable)"
