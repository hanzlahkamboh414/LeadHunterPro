"""Person Attribution Research — V1 sources.

Two sources, behind a small protocol so future Stage 3/4 sources slot in
without redesign (spec §18):

- :class:`WebsiteSource` — scoped Stage-1 crawl of people-identification pages.
- :class:`IndexedWebSource` — Stage-2 corroboration via a search API.

Both accept injectable ``fetch_page`` / ``search`` callables so unit tests run
fully deterministic with committed HTML fixtures and NO live network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from bs4 import BeautifulSoup

from app.discovery.people_parser import PeopleParser
from app.person_research.models import ResearchEvidence
from app.person_research.store import normalize_email

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: People-identification page discovery keywords, in priority order.
_PAGE_KEYWORDS = (
    "contact",
    "about",
    "team",
    "staff",
    "people",
    "leadership",
    "directory",
    "our-team",
)

#: How many pages total the Stage-1 crawl may fetch (homepage + these).
MAX_WEBSITE_PAGES = 6

MAX_INDEXED_SEARCHES = 4
MAX_INDEXED_FETCHES = 3


@dataclass
class PageFetch:
    url: str
    html: str
    ok: bool = True


@dataclass
class DomainFacts:
    """Facts from a scoped website crawl — NEVER an attribution by itself."""

    pages: list[str] = field(default_factory=list)
    #: email -> contexts where that email co-occurs with a name in a region.
    email_contexts: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    #: all names/roles found (pool — supporting, not binding).
    names: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "email_contexts": self.email_contexts,
            "names": self.names,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "DomainFacts":
        return DomainFacts(
            pages=list(d.get("pages", [])),
            email_contexts=d.get("email_contexts", {}),
            names=list(d.get("names", [])),
        )


class WebsiteSource:
    """Stage-1 scoped crawl of a company's people-identification pages.

    Strict V1 scope (spec §8): only people-identification pages, <=
    ``MAX_WEBSITE_PAGES``, single domain, robots-respecting. Not a generic
    crawler. Reuses ``PeopleParser.extract_candidates`` (adapter, read-only).
    """

    def __init__(
        self,
        *,
        fetch_page: Callable[[str], PageFetch] | None = None,
        max_pages: int = MAX_WEBSITE_PAGES,
        people_parser: PeopleParser | None = None,
    ) -> None:
        self._fetch_page = fetch_page or _default_fetch
        self._max_pages = max_pages
        self._parser = people_parser or PeopleParser()

    def discover(self, domain: str, target_email: str) -> DomainFacts:
        """Crawl the domain and return facts. Honors robots + page scoping."""
        base = _base_url(domain)
        homepage = self._fetch_page(base)
        if not homepage.ok:
            return DomainFacts()
        disallowed = _robots_disallowed(domain, self._fetch_page)

        page_urls = _discover_people_pages(homepage, base)
        page_urls = [base] + [u for u in page_urls if u != base]
        page_urls = [u for u in page_urls if not _path_disallowed(u, disallowed)]
        page_urls = page_urls[: self._max_pages]

        facts = DomainFacts(pages=page_urls)
        target = normalize_email(target_email)
        seen_contexts: set[tuple[str, str, str]] = set()

        for url in page_urls:
            fetch = self._fetch_page(url) if url != base else homepage
            if not fetch.ok:
                continue
            soup = BeautifulSoup(fetch.html, "html.parser")
            # Region-bound person <-> email pairing (the co-occurrence signal).
            for rec in self._parser.extract_candidates(
                soup, page_url=url, company_name=domain
            ):
                name = rec.person.name
                facts.names.append({"name": name, "role": rec.person.role, "page": url})
                for le in rec.emails:
                    addr = normalize_email(le.email)
                    if not addr:
                        continue
                    key = (addr, name, url)
                    if key in seen_contexts:
                        continue
                    seen_contexts.add(key)
                    facts.email_contexts.setdefault(addr, []).append(
                        {"name": name, "role": rec.person.role, "page": url}
                    )
            # Direct presence of the target email anywhere on the page.
            if target and target in _EMAIL_RE.findall(fetch.html.lower()):
                facts.email_contexts.setdefault(target, [])

        return facts


class IndexedWebSource:
    """Stage-2 corroboration via a search API (injectable for tests)."""

    def __init__(
        self,
        *,
        search: Callable[[str], list[dict[str, Any]]] | None = None,
        max_searches: int = MAX_INDEXED_SEARCHES,
    ) -> None:
        self._search = search or _default_search
        self._max_searches = max_searches

    def corroborate(self, email: str, domain: str, candidate_name: str) -> list[ResearchEvidence]:
        """Return corroboration evidence: indexed pages pairing email + name."""
        norm = normalize_email(email)
        if not norm:
            return []
        local = norm.rsplit("@", 1)[0]
        queries = _queries(local, domain, candidate_name)
        evidence: list[ResearchEvidence] = []
        last = candidate_name.split()[-1].lower() if candidate_name else ""
        for q in queries[: self._max_searches]:
            results = self._search(q) or []
            for r in results:
                url = str(r.get("url") or "").strip()
                snippet = str(r.get("snippet") or r.get("title") or "").strip()
                if not url:
                    continue
                text = snippet.lower()
                email_hit = norm.lower() in text or local.lower() in text
                name_hit = candidate_name and (
                    candidate_name.lower() in text or (last and last in text)
                )
                if email_hit and name_hit:
                    evidence.append(
                        ResearchEvidence(
                            source_url=url,
                            source_type="indexed_web",
                            authority="supporting",
                            evidence_kind="corroboration",
                            snippet=snippet[:300],
                        )
                    )
        return evidence


# ---------------------------------------------------------------------------
# default network implementations (used only when no injectable is provided)
# ---------------------------------------------------------------------------


def _default_fetch(url: str) -> PageFetch:
    import requests

    try:
        import doh_resolver  # type: ignore

        doh_resolver.install()
    except Exception:  # noqa: BLE001
        pass
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,*/*;q=0.8",
            },
            timeout=(10, 20),
            allow_redirects=True,
        )
        resp.raise_for_status()
        return PageFetch(url=url, html=resp.text, ok=True)
    except Exception:  # noqa: BLE001 - a fetch failure is a source failure, not fatal
        return PageFetch(url=url, html="", ok=False)


def _default_search(query: str) -> list[dict[str, Any]]:
    """Minimal real search hook. Returns [] unless a provider is wired in.

    Kept swappable (CLAUDE.md §4). Tests always inject a fake search.
    """
    return []


def _base_url(domain: str) -> str:
    d = (domain or "").strip().lower()
    if d.startswith(("http://", "https://")):
        return d.rstrip("/")
    return f"https://{d}"


def _discover_people_pages(homepage: PageFetch, base: str) -> list[str]:
    """Find people-identification page links from the homepage (scoped)."""
    soup = BeautifulSoup(homepage.html, "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = (a.get_text(" ", strip=True) or "").lower()
        combined = (href + " " + text).lower()
        if not any(k in combined for k in _PAGE_KEYWORDS):
            continue
        url = _resolve(href, base)
        if url and url not in seen and url.startswith(base):
            seen.add(url)
            found.append(url)
    return found


def _resolve(href: str, base: str) -> str | None:
    if href.startswith(("http://", "https://")):
        return href.split("#")[0]
    if href.startswith("/"):
        return base + href.split("#")[0]
    return None


def _robots_disallowed(domain: str, fetch: Callable[[str], PageFetch]) -> list[str]:
    try:
        pg = fetch(f"https://{domain}/robots.txt")
    except Exception:  # noqa: BLE001
        return []
    if not pg.ok:
        return []
    disallowed: list[str] = []
    for line in pg.html.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("disallow:") and len(line) > len("disallow:"):
            disallowed.append(line.split(":", 1)[1].strip())
    return disallowed


def _path_disallowed(url: str, disallowed: list[str]) -> bool:
    if not disallowed:
        return False
    from urllib.parse import urlparse

    path = urlparse(url).path
    return any(path.startswith(d) for d in disallowed if d)


def _queries(local: str, domain: str, candidate_name: str) -> list[str]:
    qs = [f'"{local}@{domain}"', f"site:{domain} {local}"]
    last = candidate_name.split()[-1] if candidate_name else ""
    if last:
        qs.append(f"{domain} team staff about {last}")
    return qs
