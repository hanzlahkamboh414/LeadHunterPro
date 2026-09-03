"""Plan-holder / bid-holder PDF discovery source (lead-pipeline increment 2).

WHAT THIS SOLVES
----------------
When a public project goes to bid, the agency publishes a "Plan Holder
List" PDF: every contractor that pulled the plans, with a named contact,
an email and a phone per company. That one artifact carries proven
participation + a decision maker + a public email — precisely the raw
material the V1 lead gate is starved of (Stage 1 / M3: "no named
decision-maker", "no person_bound email"). :class:`PdfPlanHolderParser`
(Inc 1) already turns one such PDF into rows; this source finds the PDFs
live and feeds those rows into the discovery pipeline as a replaceable
:class:`~app.discovery.sources.base_source.BaseSource`.

THE PIPELINE (all existing pieces, rule #14 — no new engine)
------------------------------------------------------------
    query (industry/location)
      -> validated dorks   ("Plan Holder List" filetype:pdf, ...)
      -> search registry   (app.search_providers, auto-registered from .env)
      -> .pdf URL post-filter (Tavily is semantic and ignores filetype:pdf)
      -> PDF fetch         (requests + best-effort DoH DNS repair)
      -> PdfPlanHolderParser -> PlanHolderRow[]
      -> one company record per row (person/emails/phones/date in metadata)

HONEST LIMITS, STATED UP FRONT
------------------------------
- A plan-holder row carries NO verified website. The record's ``website``
  is DERIVED from the row's real company email domain (e.g. a real public
  record ``name@pirctobin.com`` -> ``https://pirctobin.com``). It is a real
  domain, never a placeholder (CLAUDE.md §12), but it is derived, not
  crawled — so ``gate_accepted=False`` on every record this source emits
  and the data flows through the unverified path, exactly as the connector
  handles any not-yet-verified candidate.
- ``role`` stays empty and ``role_relevance=False`` on every person: a
  plan-holder list carries no job title, and no role is invented or
  backfilled (hard rule #2). The V1 gate therefore still blocks these rows
  on role until a later increment enriches it — this source is a sourcing
  FEED, not a qualification fix.
- Free-mail-only rows keep their website empty (``aol.com``/``gmail.com``
  are not a company domain) and stay flagged ``free_mail_only=True``.

STATUS CONTRACT (BaseSource)
----------------------------
SUCCESS      — at least one company record emitted from parsed rows.
EMPTY        — no .pdf URLs surfaced, or PDFs parsed but produced no rows.
UNAVAILABLE  — .pdf URLs found but every fetch failed (network/DNS).
ERROR        — unexpected exception (never raised to the orchestrator).
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import sys
from typing import Any, Callable
from urllib.parse import urlparse

from app.discovery.pdf_plan_holder_parser import (
    PlanHolderRow,
    PdfPlanHolderParser,
)
from app.discovery.sources.base_source import BaseSource
from app.discovery.sources.status import SourceStatus
from app.email.email_cleaner import is_free_mail_domain

logger = logging.getLogger(__name__)

#: Validated plan-holder dork templates. ``{industry}`` / ``{location}`` are
#: injected from the query. One pattern is enough to ship (Inc 2 scope); a
#: wider vocabulary ("Bidders List", "Plan Room", "Prospective Bidders") is a
#: logged backlog item, not this increment.
_DORK_TEMPLATES = (
    '"Plan Holder List" filetype:pdf {industry} {location}',
    '"Plan Holders List" filetype:pdf {industry} {location}',
    '"Bid Holders List" filetype:pdf {industry} {location}',
    '"List of Plan Holders" filetype:pdf {industry} {location}',
)

#: Bounded fan-out: at most this many PDFs are fetched per discover() call so
#: a search that surfaces a pile of PDFs cannot explode the run (Blueprint §4
#: bounded-crawl discipline, same guardrail as DirectoryCrawlSource).
MAX_PDFS = 5

#: Default results asked of each provider per dork.
RESULTS_PER_DORK = 10

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
#: (connect, read) — same rationale as the diagnostic scripts.
_TIMEOUT = (6, 25)


def _is_pdf(url: str) -> bool:
    """True when the URL points at a PDF (path suffix or query carries .pdf)."""
    lowered = url.lower()
    return ".pdf" in urlparse(lowered).path or ".pdf" in lowered


def _host(url: str) -> str:
    """Host without the ``www.`` prefix, for provenance labels."""
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except ValueError:
        return ""


class PlanHolderSource(BaseSource):
    """Find plan-holder PDFs live and feed their rows into discovery.

    Every network stage is behind an injectable seam so tests run fully
    offline: ``search`` (dorks -> search results), ``fetch`` (url -> bytes),
    and ``parser`` (bytes -> rows). The defaults reuse the existing search
    registry, a requests + DoH-DNS transport, and the Inc-1 parser.
    """

    source_name = "plan_holder"
    description = "Plan-holder / bid-holder PDF lists found via live search"
    # 25 sits between DirectoryCrawlSource (20, the API-free primary) and the
    # search providers (50) / fixture bridge (999). Higher = tried later, on a
    # 0-100 scale for the live sources (fixture is the 999 emergency tail).
    priority = 25
    enabled = True

    def __init__(
        self,
        *,
        search: Callable[[list[str]], list[Any]] | None = None,
        fetch: Callable[[str], bytes | None] | None = None,
        parser: PdfPlanHolderParser | None = None,
        dork_templates: tuple[str, ...] | None = None,
    ) -> None:
        """Configure the source.

        Args:
            search: ``(dorks) -> SearchResult-like list``. Defaults to running
                the dorks through the existing search registry.
            fetch: ``(url) -> pdf bytes``, or None on failure. Defaults to a
                requests + DoH-DNS transport.
            parser: PDF -> rows. Defaults to a real :class:`PdfPlanHolderParser`.
            dork_templates: Dork templates (``{industry}``/``{location}``
                placeholders). Defaults to the validated plan-holder set.
        """
        self._search = search
        self._fetch = fetch
        self._parser = parser if parser is not None else PdfPlanHolderParser()
        self._dork_templates = (
            dork_templates if dork_templates is not None else _DORK_TEMPLATES
        )

    # -- BaseSource -------------------------------------------------------

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        """Discover companies from live plan-holder PDFs.

        Never raises: an unexpected error is returned as ``ERROR`` so the
        orchestrator can continue with the next source (orchestrator contract).
        """
        try:
            return self._discover(industry=industry, location=location, limit=limit)
        except Exception as exc:  # noqa: BLE001 — never propagate to orchestrator
            logger.exception("PlanHolderSource: unexpected error")
            return SourceStatus.ERROR, [], {
                "source": self.source_name,
                "error": f"plan-holder discovery failed: {exc}",
            }

    def _discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        if limit <= 0:
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "reason": "limit_zero",
            }

        dorks = self._build_dorks(industry, location)
        pdf_urls = self._search_pdfs(dorks)
        if not pdf_urls:
            # CLAUDE.md §5: never go silent about WHY live discovery failed.
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "reason": "no_pdf_results",
                "dorks": dorks,
                "industry": industry,
                "location": location,
            }

        targeted = pdf_urls[:MAX_PDFS]
        rows: list[PlanHolderRow] = []
        fetch_failures = 0
        fetched = 0
        parse_unreadable = 0
        for url in targeted:
            data = self._fetch_bytes(url)
            if data is None:
                fetch_failures += 1
                continue
            fetched += 1
            result = self._parser.parse(data, source_url=url)
            if result.rows:
                rows.extend(result.rows)
            else:
                # A reachable PDF with no usable rows is not a fetch failure —
                # it is a layout/scan mismatch we cannot extract from yet.
                parse_unreadable += 1

        if fetch_failures == len(targeted) and not rows:
            # Every PDF was found in search but none could be fetched — the
            # source is configured but unreachable right now (honest UNAVAILABLE).
            return SourceStatus.UNAVAILABLE, [], {
                "source": self.source_name,
                "reason": "all_fetches_failed",
                "pdfs_found": len(pdf_urls),
                "pdfs_targeted": len(targeted),
                "fetch_failures": fetch_failures,
                "dorks": dorks,
            }

        records = self._dedup([self._to_company_record(r) for r in rows])
        if not records:
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "reason": "no_rows_in_pdfs",
                "pdfs_found": len(pdf_urls),
                "pdfs_fetched": fetched,
                "pdfs_unreadable": parse_unreadable,
                "rows_raw": len(rows),
            }

        metadata: dict[str, Any] = {
            "source": self.source_name,
            "data_source": "live",
            "status": SourceStatus.SUCCESS,
            "dorks": dorks,
            "pdfs_found": len(pdf_urls),
            "pdfs_fetched": fetched,
            "pdfs_failed": fetch_failures,
            "pdfs_unreadable": parse_unreadable,
            "rows_raw": len(rows),
            "rows_emitted": len(records),
            "industry": industry,
            "location": location,
            "limit": limit,
        }
        logger.info(
            "PlanHolderSource: %s — %d rows from %d PDFs (%d found, %d fetched, "
            "%d failed, %d unreadable)",
            SourceStatus.SUCCESS.value,
            len(records),
            len(rows),
            len(pdf_urls),
            fetched,
            fetch_failures,
            parse_unreadable,
        )
        return SourceStatus.SUCCESS, records[:limit], metadata

    async def health_check(self) -> dict[str, Any]:
        """Report configuration health without performing I/O.

        The source needs only a search provider configured; whether one is
        present is read from the registry. A missing provider makes this
        source EMPTY, not broken, so health reflects configuration only.
        """
        try:
            from app.search_providers.registry import get_registry

            enabled = get_registry().get_enabled()
        except Exception:  # noqa: BLE001 — a registry hiccup must not crash health
            enabled = []
        return {
            "healthy": True,
            "source": self.source_name,
            "providers_registered": len(enabled),
            "note": "Live only when a search provider is configured",
        }

    # -- dork + search ----------------------------------------------------

    def _build_dorks(self, industry: str, location: str) -> list[str]:
        """Expand the dork templates with the query terms, deduplicated."""
        industry = (industry or "").strip()
        location = (location or "").strip()
        dorks: list[str] = []
        for template in self._dork_templates:
            text = template.format(industry=industry, location=location)
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in dorks:
                dorks.append(text)
        return dorks

    def _search_pdfs(self, dorks: list[str]) -> list[str]:
        """Run the dorks and post-filter to .pdf URLs, preserving order.

        A semantic engine (Tavily) ignores ``filetype:pdf``, so the filter is
        applied to the returned URLs here — the validated lesson of the dork
        probe (pdf_ratio tells the engine apart, it is not a result filter).
        """
        results = self._search(dorks) if self._search else self._default_search(dorks)
        seen: set[str] = set()
        urls: list[str] = []
        for result in results:
            url = getattr(result, "url", None) or ""
            if not url or not _is_pdf(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
        return urls

    def _default_search(self, dorks: list[str]) -> list[Any]:
        """Run ALL dorks through the search manager in ONE event loop.

        Imported here, not at module scope, to avoid an import cycle with the
        provider registry auto-registration.

        ONE loop is the point: the Tavily provider holds an ``aiohttp``
        session, and a per-dork ``asyncio.run`` would bind that session to the
        first loop, then fail every later dork with "Event loop is closed"
        when the next ``asyncio.run`` spins up a fresh loop. Provider sessions
        are closed inside the same loop so nothing leaks a closed-loop
        connector (re-opening is lazy and safe).
        """
        from app.search_providers.manager import SearchProviderManager
        from app.search_providers.models import SearchQuery
        from app.search_providers.registry import get_registry

        manager = SearchProviderManager()

        async def _run_all() -> list[Any]:
            collected: list[Any] = []
            try:
                for dork in dorks:
                    response = await manager.search(
                        SearchQuery(keywords=dork, num_results=RESULTS_PER_DORK)
                    )
                    collected.extend(response.results)
            finally:
                for provider in get_registry().get_enabled():
                    close = getattr(provider, "close", None)
                    if close:
                        try:
                            await close()
                        except Exception:  # noqa: BLE001 - closing is best-effort
                            pass
            return collected

        return asyncio.run(_run_all())

    # -- fetch + parse ----------------------------------------------------

    def _fetch_bytes(self, url: str) -> bytes | None:
        """Fetch ``url``; return the PDF bytes, or None on any failure.

        One dead PDF must not abort discovery of the others; the caller
        counts failures and reports UNAVAILABLE only when every fetch fails.
        """
        try:
            if self._fetch:
                return self._fetch(url)
            return self._default_fetch(url)
        except Exception as exc:  # noqa: BLE001 — one PDF never kills the run
            logger.info("PlanHolderSource: %s unreachable — %s", url, exc)
            return None

    @staticmethod
    def _default_fetch(url: str) -> bytes:
        """requests GET with best-effort DoH DNS repair (reuses the transport
        the diagnostic scripts use). The repair is inert when the host
        resolver answers, so it adds no failure mode for healthy hosts."""
        try:
            _SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
            if str(_SCRIPTS) not in sys.path:
                sys.path.insert(0, str(_SCRIPTS))
            import doh_resolver  # type: ignore  # noqa: PLC0415

            doh_resolver.install()  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — DNS repair is best-effort
            pass

        import requests  # noqa: PLC0415

        resp = requests.get(
            url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True
        )
        resp.raise_for_status()
        return resp.content

    # -- row -> company record ---------------------------------------------

    def _to_company_record(self, row: PlanHolderRow) -> dict[str, Any]:
        """Project one plan-holder row onto the source company schema.

        The connector's step-3 filters and ``_normalize_company`` adapter
        consume ``company_name``/``website``/``source_url``/``data_provenance``
        like any other source record (CLAUDE.md §4: every source replaceable).
        The plan-holder person/emails/phones/date ride in ``metadata["plan_holder"]``
        so a later increment (Inc 3) can bridge the pre-bound person into the
        LeadPipeline without re-crawling the site — and so nothing is lost here.
        """
        emails = [
            {"email": e.email, "tier": e.tier.value, "source_url": e.source_url}
            for e in row.emails
        ]
        phones = [
            {"phone": p.phone, "tier": p.tier.value, "source_url": p.source_url}
            for p in row.phones
        ]
        person = None
        if row.person is not None:
            person = {
                "name": row.person.name,
                "role": row.person.role,  # "" for a plan-holder list — never backfilled
                "role_relevance": row.person.role_relevance,  # False — hard rule #2
                "tier": row.person.tier.value,
                "source_url": row.person.source_url,
            }
        domain = self._domain_from_emails(emails)
        host = _host(row.source_url)
        return {
            "company_name": row.company,
            "website": self._derive_website(domain),
            "city": "",
            "state": "",
            "country": "USA",
            "trade_category": "",
            "industry_focus": "",
            "revenue_tier": "",
            "source_url": row.source_url,
            "data_provenance": f"plan_holder_pdf:{host}" if host else "plan_holder_pdf",
            "discovery_reason": f"Listed on plan-holder list ({row.source_url})",
            "confidence": 0.5,
            # Nothing this source emits is verified — the V1 gate's first rule
            # (AcceptanceGate-verified company) stays unsatisfied until a later
            # stage verifies the company.
            "gate_accepted": False,
            "plan_holder": {
                "person": person,
                "emails": emails,
                "phones": phones,
                "date_contacted": row.date_contacted,
                "free_mail_only": row.free_mail_only,
                "local_part_matches_name": row.local_part_matches_name,
                "domain": domain,
                "page": row.page,
                "source_url": row.source_url,
            },
        }

    @staticmethod
    def _domain_from_emails(emails: list[dict[str, Any]]) -> str:
        """The shared registered domain across the row's emails, if any.

        Used as the record's dedup key and for the derived website. Returns ""
        for a free-mail-only row (aol.com/gmail.com are not a company domain).
        """
        for entry in emails:
            address = entry.get("email") or ""
            if "@" in address:
                domain = address.rsplit("@", 1)[-1].lower().strip()
                if domain and not is_free_mail_domain(domain):
                    return domain
        return ""

    @staticmethod
    def _derive_website(domain: str) -> str:
        """``https://<domain>`` when the row has a real company domain, else "".

        The domain comes from a real public-record email address, so this is
        a genuine domain (never a placeholder, §12) — but it is DERIVED, not
        crawled/verified, which is why the record is ``gate_accepted=False``.
        """
        return f"https://{domain}" if domain else ""

    @staticmethod
    def _dedup(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop intra-source duplicates by registered domain (or name when the
        row has no company domain, e.g. free-mail-only).

        A plan-holder list can carry the same contractor across multiple
        pages, and two PDFs can list the same firm. Cross-source dedup is the
        orchestrator's job (domain+name merge); this is only the obvious
        same-source duplicate guard at creation time. First-seen wins.
        """
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for record in records:
            domain = (record.get("plan_holder") or {}).get("domain") or ""
            key = domain or (record.get("company_name") or "").strip().lower()
            if not key:
                continue
            if key in seen:
                continue
            seen.add(key)
            unique.append(record)
        return unique
