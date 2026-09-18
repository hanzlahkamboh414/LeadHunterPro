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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import urlparse

from app.discovery.pdf_plan_holder_parser import (
    STATUS_TOO_LARGE,
    PlanHolderRow,
    PdfPlanHolderParser,
)
from app.discovery.sources.base_source import BaseSource
from app.discovery.sources.status import SourceStatus
from app.discovery.yield_learning import segment_key
from app.email.email_cleaner import is_free_mail_domain
from app.engines.verification.location_verifier import (
    _US_STATES,
    _extract_mentions,
    _parse_target,
)

logger = logging.getLogger(__name__)

#: Validated plan-holder dork templates. ``{industry}`` / ``{location}`` are
#: injected from the query. Every template names a public document that lists
#: CONTRACTORS with contacts (plan-holder / bidder rosters) — never notices or
#: results pages, which list the AGENCY's contact and would pollute leads.
#: The vocabulary is deliberately wider than the original single pattern so a
#: pass can advance through LATER templates once earlier dorks surface nothing
#: new (each engine phrasing yields a different document set).
_DORK_TEMPLATES = (
    '"Plan Holder List" filetype:pdf {industry} {location}',
    '"Plan Holders List" filetype:pdf {industry} {location}',
    '"Bid Holders List" filetype:pdf {industry} {location}',
    '"List of Plan Holders" filetype:pdf {industry} {location}',
    '"Prospective Bidders" filetype:pdf {industry} {location}',
    '"Bidders List" filetype:pdf {industry} {location}',
    '"Plan Room" filetype:pdf {industry} {location}',
    '"Plan Holder Report" filetype:pdf {industry} {location}',
)

#: Bounded fan-out: at most this many PDFs are fetched per discover() call so
#: a search that surfaces a pile of PDFs cannot explode the run (Blueprint §4
#: bounded-crawl discipline, same guardrail as DirectoryCrawlSource). 10 (was
#: 5) is the per-pass working set: discovery ADVANCES pass to pass by skipping
#: already-parsed PDFs (``skip_pdfs``), so a bigger batch per pass means more
#: distinct documents get examined before the source honestly reports exhaust.
MAX_PDFS = 10

#: Largest body worth pulling over the wire. The parser refuses documents past
#: ``MAX_PAGES``, so this is the same bound applied one layer earlier: stop a
#: huge response at the socket instead of buffering it into memory to discover
#: it is huge. Every real plan-holder list parsed so far is far under 2 MB (the
#: 4-page HR Green fixture is ~100 KB), so 12 MB is ~120x headroom — it exists
#: to refuse a spec book, not to trim a large roster.
MAX_PDF_BYTES = 12 * 1024 * 1024

#: Default results asked of each provider per dork. Was 5, raised to 10 with
#: MAX_PDFS: the lazy loop only ever surfaces unseen PDFs, so a richer result
#: set lets a single dork feed several passes instead of re-spending credits
#: on repeated queries.
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


def _result_text(result: Any) -> str:
    """Title + snippet of a search result, as one text blob for the location gate.

    The gate reasons about a PDF WITHOUT fetching it: the search engine's own
    snippet usually names the state a document covers (its title is often the
    agency/dot name, its snippet often "…North Carolina DOT…"). Falling back to
    the bare snippet when there is no title keeps the url-only fakes green.
    """
    title = getattr(result, "title", "") or ""
    snippet = getattr(result, "snippet", "") or ""
    return f"{title}\n{snippet}".strip()


def _order_by_location(
    pdf_urls: list[str],
    candidate_text: dict[str, str],
    target_state: str | None,
) -> tuple[list[str], int]:
    """Gate candidate plan-holder PDFs by query region BEFORE fetch/parse.

    Location Fix (Phase D): a plan-holder PDF for a DIFFERENT state leaks
    nationwide junk (NYC/ND/KS/IN/FL/MD/IL/OK DOT lists) and out-of-region
    agencies (S.Carolina DOT) into a Houston run — a credit burn plus a
    relevance failure. This reuses the deterministic ``location_verifier``
    state extractor on each candidate's search title/snippet:

      rank 2 — snippet names the target state  -> KEEP, dispatch first
      rank 0 — snippet names a DIFFERENT state -> out-of-region, DROP
      rank 1 — no state signal                -> KEEP (conservative: never
               reject without evidence)

    A snippet naming BOTH the target and another state stays rank 2 (only a
    set of states WITHOUT the target drops). The query has no parseable state
    (e.g. a bare city) -> nothing is rejected. On-target PDFs are ordered
    before neutral ones so MAX_PDFS favours in-region leads.

    Returns (kept_urls, dropped_count). dropped_count is surfaced in metadata
    (CLAUDE.md §6 — never silent).
    """
    if not target_state or not pdf_urls:
        return pdf_urls, 0
    # ``location_verifier._extract_mentions`` deliberately reads a state CODE
    # only in comma-form ("Dallas, TX") — a bare code is ambiguous for
    # VERIFYING where a company is. A document-REGION gate is different: we
    # know the one target we care about, so scanning specifically for THAT bare
    # uppercase code (e.g. ``\\bTX\\b``) is safe and closes the gap that made a
    # "Houston TX project bidders" snippet register nothing.
    target_code_re = re.compile(rf"\b{re.escape(target_state)}\b")
    target_name_re = re.compile(
        rf"\b{re.escape(_US_STATES.get(target_state, target_state))}\b",
        re.IGNORECASE,
    )
    scored: list[tuple[int, str]] = []
    for url in pdf_urls:
        text = (candidate_text.get(url) or "").strip()
        rank = 1  # no signal -> keep (conservative)
        if text:
            mentions, _rejected = _extract_mentions(text)
            states = {s for _, s in mentions if s}
            mentions_target = (
                target_state in states
                or bool(target_code_re.search(text))
                or bool(target_name_re.search(text))
            )
            mentions_other = any(s != target_state for s in states)
            if mentions_target:
                rank = 2  # in-region -> keep, dispatch first
            elif mentions_other:
                rank = 0  # names a different state -> out-of-region junk
        scored.append((rank, url))
    scored.sort(key=lambda item: -item[0])  # stable: order preserved within rank
    kept = [url for rank, url in scored if rank > 0]
    return kept, len(scored) - len(kept)


def _content_out_of_region(state_codes: list[str], target_state: str | None) -> bool:
    """Gate a PARSED document's rows against the query region.

    The snippet gate (:func:`_order_by_location`) reads only search-result title
    + snippet, which often lacks the state name altogether (a nationwide bid
    list's snippet is "Plan holders for General Contractors", no state). This
    content gate reads the document's OWN parsed text via the state codes the
    parser exposed, so a list whose body names a non-target state — Clark County
    *Washington*, *New York City* DCAS, a South Dakota newspaper — is caught even
    when its snippet was silent.

    Rule (conservative, never false-drops an in-region list): out-of-region ONLY
    when the document names at least one state AND none of them is the target.
    A document the extractor reads as location-silent names no state -> kept.
    A document naming Texas/TX for a ``San Antonio TX`` query -> kept.
    """
    if not target_state or not state_codes:
        return False
    return target_state not in set(state_codes)


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
        yield_store: Any | None = None,
        candidate_store: Any | None = None,
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
            yield_store: OPTIONAL :class:`~app.discovery.yield_learning.
                DiscoveryYieldStore` for the Layer-1 dork-yield loop (Phase G).
                When present, a dork template already proven (MIN_TRIALS
                dispatches, zero working leads) is NOT dispatched, and every
                actually-dispatched dork records a trial. Companies carry the
                producing dork's label so research can credit ``working``.
            candidate_store: OPTIONAL :class:`~app.discovery.template_candidates.
                TemplateCandidateStore` for Phase H. When present (with a
                ``yield_store``), LLM-GENERATED dork templates that have not
                been proven dead join the dispatch set after the static dorks —
                the add-side of the self-learning loop.
        """
        self._search = search
        self._fetch = fetch
        self._parser = parser if parser is not None else PdfPlanHolderParser()
        self._dork_templates = (
            dork_templates if dork_templates is not None else _DORK_TEMPLATES
        )
        self._yield_store = yield_store
        self._candidate_store = candidate_store
        # Per-call search diagnostics: provider failures surfaced by the lazy
        # default search, reset at the top of every discover() so a stale
        # failure from an earlier pass never leaks into a later one.
        self._last_search_errors: list[str] = []
        # Per-call candidate text (title+snippet per PDF URL) threaded from the
        # search into _discover for the Location-Fix gate — filled by whichever
        # search path ran (seam or default), read once after _search_pdfs.
        self._last_candidate_text: dict[str, str] = {}

    # -- BaseSource -------------------------------------------------------

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
        skip_pdfs: set[str] | None = None,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        """Discover companies from live plan-holder PDFs.

        ``skip_pdfs``: source_urls already parsed in this run (or earlier in
        this multi-pass loop). Passed into the search so a pass ADVANCES to
        unseen documents instead of re-pulling the same PDFs — the difference
        between a pass that finds new leads and one that tautologically returns
        the rows it already produced. An exhausted pass (nothing unseen left to
        offer) reports EMPTY with ``reason="no_unseen_pdfs"`` so the caller can
        stop honestly instead of burning credits on repeats.

        Never raises: an unexpected error is returned as ``ERROR`` so the
        orchestrator can continue with the next source (orchestrator contract).

        Phase I: the (industry, location) pair is normalized into a SEGMENT
        (``segment_key``) and threaded through dispatch + yield credit, so the
        learned gates act per (trade, location) instead of globally.
        """
        try:
            return self._discover(
                industry=industry, location=location, limit=limit,
                skip_pdfs=skip_pdfs,
                segment=segment_key(industry, location),
            )
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
        skip_pdfs: set[str] | None = None,
        segment: str = "",
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        if limit <= 0:
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "reason": "limit_zero",
            }

        # Reset per-call search diagnostics BEFORE the search so a stale
        # failure from a previous pass never leaks into this one.
        self._last_search_errors: list[str] = []
        skip = set(skip_pdfs or ())
        dork_pairs = self._build_dork_pairs(industry, location, segment)
        dorks = [d for _, d in dork_pairs]
        # (pdf_urls, url_to_dork): the second maps each surfaced PDF back to the
        # dork TEMPLATE that produced it, so every company record can carry its
        # producing dork for Layer-1 yield attribution (Phase G). ``segment``
        # (the normalized trade|location) is threaded into the yield gate so a
        # dork proven dead FOR THIS SEGMENT is not dispatched here (Phase I).
        pdf_urls, url_to_dork = self._search_pdfs(dork_pairs, skip=skip, segment=segment)
        search_errors = list(self._last_search_errors)
        if not pdf_urls:
            # CLAUDE.md §5: never go silent about WHY live discovery failed.
            # A URL-less pass is NOT automatically "no results": when every
            # dork search ERRORED (provider down / quota-exhausted), the honest
            # status is UNAVAILABLE with the exact provider errors — not a
            # quiet "no_pdf_results" that reads as "nothing exists on the web"
            # (§12: hidden fallback / silent failure is forbidden). ``no_unseen_pdfs``
            # distinguishes real exhaustion (every surfaced PDF already parsed)
            # from an honestly-empty first search.
            if search_errors:
                return SourceStatus.UNAVAILABLE, [], {
                    "source": self.source_name,
                    "reason": "search_failed",
                    "search_errors": search_errors,
                    "dorks": dorks,
                    "industry": industry,
                    "location": location,
                }
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "reason": "no_unseen_pdfs" if skip else "no_pdf_results",
                "search_errors": search_errors,
                "pdfs_already_parsed": len(skip),
                "dorks": dorks,
                "industry": industry,
                "location": location,
            }

        # Location Fix (Phase D): gate candidate PDFs by query region BEFORE any
        # fetch/parse. Rank-0 (names a different state) documents are dropped
        # and on-target docs ordered first, so MAX_PDFS favours in-region leads
        # instead of spending fetch+parse credits on nationwide junk. Nothing
        # knows the target state (bare-city query) -> all kept, no risk.
        raw_pdf_urls = pdf_urls
        _candidate_city, target_state = _parse_target(location)
        pdf_urls, location_dropped = _order_by_location(
            raw_pdf_urls, (self._last_candidate_text or {}), target_state,
        )
        if not pdf_urls:
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "reason": "all_pdfs_out_of_region",
                "pdfs_found": len(raw_pdf_urls),
                "pdfs_location_dropped": location_dropped,
                "target_state": target_state,
                "dorks": dorks,
                "industry": industry,
                "location": location,
            }

        targeted = pdf_urls[:MAX_PDFS]
        rows: list[PlanHolderRow] = []
        fetch_failures = 0
        fetched = 0
        parse_unreadable = 0
        parse_too_large = 0
        # Galti #2 relevance gate: rows with NO email. A plan-holder row with an
        # email is a contactable company; one without (phone/company-only from a
        # giant spec, RFP attachment, council agenda or newsletter) cannot clear
        # the qualification gate — researching it guarantees a skip and spends
        # credits. Every real list we parse (hrgreen, SCDOT, BSE, RCTLMA) emits
        # email-carrying rows, so this drops only noise. Counted, never silent.
        email_less = 0
        # Content-level location gate (Phase D, content pass): an in-region
        # snippet can STILL be a nationwide list, so after parse we re-gate by
        # the states the document's own text names. Same conservative rule as
        # the snippet gate — drop ONLY a document that explicitly names a
        # non-target state; a location-silent document is kept (no false drops).
        content_dropped = 0

        # Fetch the working set concurrently (bounded workers) so a pass costs
        # ~one slow-PDF latency instead of the sum of every PDF's timeout.
        with ThreadPoolExecutor(max_workers=3) as pool:
            fetched_data = list(pool.map(self._fetch_bytes, targeted))

        for url, data in zip(targeted, fetched_data):
            if data is None:
                fetch_failures += 1
                continue
            fetched += 1
            result = self._parser.parse(data, source_url=url)
            if result.rows:
                if _content_out_of_region(result.report.state_codes, target_state):
                    content_dropped += 1
                    continue
                kept = [r for r in result.rows if r.emails]
                email_less += len(result.rows) - len(kept)
                rows.extend(kept)
            else:
                # A reachable PDF with no usable rows is not a fetch failure —
                # it is a layout/scan mismatch we cannot extract from yet.
                # A document past MAX_PAGES is counted SEPARATELY: "too long to
                # read" and "read, no table" are different facts, and merging
                # them would hide a dork that keeps surfacing spec books behind
                # a number that looks like ordinary template drift (§6).
                if result.report.parse_status == STATUS_TOO_LARGE:
                    parse_too_large += 1
                else:
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

        records = self._dedup([
            self._to_company_record(r, discovery_dork=url_to_dork.get(r.source_url, ""))
            for r in rows
        ])
        if not records:
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "reason": "no_rows_in_pdfs",
                "pdfs_found": len(pdf_urls),
                "pdfs_location_dropped": location_dropped,
                "pdfs_fetched": fetched,
                "pdfs_unreadable": parse_unreadable,
                "pdfs_too_large": parse_too_large,
                "pdfs_content_dropped": content_dropped,
                "target_state": target_state,
                "rows_raw": len(rows),
            }

        metadata: dict[str, Any] = {
            "source": self.source_name,
            "data_source": "live",
            "status": SourceStatus.SUCCESS,
            "dorks": dorks,
            "pdf_urls": pdf_urls,
            "pdfs_found": len(raw_pdf_urls),
            "pdfs_location_dropped": location_dropped,
            "target_state": target_state,
            "pdfs_skipped": len(skip),
            "pdfs_fetched": fetched,
            "pdfs_failed": fetch_failures,
            "pdfs_unreadable": parse_unreadable,
            "pdfs_too_large": parse_too_large,
            "pdfs_content_dropped": content_dropped,
            "rows_raw": len(rows),
            "rows_email_less_dropped": email_less,
            "rows_emitted": len(records),
            "industry": industry,
            "location": location,
            "limit": limit,
        }
        logger.info(
            "PlanHolderSource: %s — %d rows from %d PDFs (%d found, %d "
            "location-dropped, %d content-dropped, %d fetched, %d failed, %d "
            "unreadable, %d too large, %d email-less dropped)",
            SourceStatus.SUCCESS.value,
            len(records),
            len(rows),
            len(raw_pdf_urls),
            location_dropped,
            content_dropped,
            fetched,
            fetch_failures,
            parse_unreadable,
            parse_too_large,
            email_less,
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

    def _effective_dork_templates(self, segment: str = "") -> tuple[str, ...]:
        """Static dorks + LLM-generated candidates not proven dead (Phase H).

        ``candidate_store.effective_dorks(yield_store, segment)`` returns the
        dispatch-eligible generated patterns; the yield store is the single
        source of truth (a candidate dispatches unless proven dead FOR THE
        SEGMENT). Without a candidate store the set is exactly the static dorks
        — the pre-Phase-H behavior, untouched.
        """
        if self._candidate_store is None:
            return self._dork_templates
        try:
            generated = self._candidate_store.effective_dorks(
                self._yield_store, segment=segment,
            ) or ()
        except Exception:  # noqa: BLE001 — a broken candidate store must never
            logger.warning("PlanHolderSource: candidate store unavailable", exc_info=True)
            return self._dork_templates
        # Dedup against statics (a candidate can never shadow a known dork).
        known = set(self._dork_templates)
        added = tuple(g for g in generated if g not in known)
        if added:
            logger.info(
                "PlanHolderSource: dispatching %d generated dork(s) after statics",
                len(added),
            )
        return self._dork_templates + added

    def _build_dork_pairs(
        self, industry: str, location: str, segment: str = "",
    ) -> list[tuple[str, str]]:
        """Expand the dork templates into ``(template_label, filled_dork)`` pairs.

        The LABEL is the raw template (with ``{industry}``/``{location}``
        placeholders) — the STABLE learning key for Phase G yield, so a dork's
        yield is measured across every query, never per concrete spelling.
        ``segment`` (Phase I) is the normalized trade|location the running
        query belongs to; it gates which candidate dorks dispatch.
        """
        industry = (industry or "").strip()
        location = (location or "").strip()
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for template in self._effective_dork_templates(segment):
            text = template.format(industry=industry, location=location)
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in seen:
                seen.add(text)
                pairs.append((template, text))
        return pairs

    def _build_dorks(self, industry: str, location: str) -> list[str]:
        """Expand the dork templates with the query terms, deduplicated."""
        return [d for _, d in self._build_dork_pairs(industry, location)]

    def _search_pdfs(
        self, dork_pairs: list[tuple[str, str]], *, skip: set[str] | None = None,
        segment: str = "",
    ) -> tuple[list[str], dict[str, str]]:
        """Run the dorks and post-filter to .pdf URLs, preserving order.

        Returns ``(pdf_urls, url_to_dork)``. A semantic engine (Tavily) ignores
        ``filetype:pdf``, so the filter is applied to the returned URLs here —
        the validated lesson of the dork probe. ``skip`` (source_urls already
        parsed in this run) is honoured so a pass only collects documents that
        can actually ADVANCE discovery.

        ``segment`` (Phase I) is the normalized ``segment_key`` of the running
        query; it flows to the yield gate so a dork dead for one segment still
        dispatches for another.

        The injected ``_search`` seam keeps its all-dorks-at-once contract, but
        returns no per-dork signal — its ``url_to_dork`` is empty (no yield
        attribution, silence not evidence). The default live path is lazy: dorks
        run one at a time and stop as soon as MAX_PDFS unseen PDF URLs are
        collected, so a dork that already surfaces enough PDFs does not spend
        extra search credits on the remaining dorks.
        """
        if self._search:
            results = self._search([d for _, d in dork_pairs])
            # Stash each surfaced PDF's title+snippet so _discover can gate by
            # location without re-fetching. Extra keys (non-PDF / already-seen)
            # are harmless: _order_by_location only looks up URLs actually kept.
            self._last_candidate_text = {
                url: _result_text(r)
                for r in results
                if (url := getattr(r, "url", "")) and _is_pdf(url)
            }
            return self._filter_pdfs(results, skip=skip), {}
        return self._default_search_pdfs(dork_pairs, skip=skip, segment=segment)

    @staticmethod
    def _filter_pdfs(results: list[Any], *, skip: set[str] | None = None) -> list[str]:
        """Post-filter search results to deduplicated, unseen .pdf URLs, in order."""
        seen: set[str] = set(skip or ())
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

    def _default_search_pdfs(
        self, dork_pairs: list[tuple[str, str]], *, skip: set[str] | None = None,
        segment: str = "",
    ) -> tuple[list[str], dict[str, str]]:
        """Run the dorks lazily through the search manager in ONE event loop.

        Imported here, not at module scope, to avoid an import cycle with the
        provider registry auto-registration.

        ONE loop is the point: the Tavily provider holds an ``aiohttp``
        session, and a per-dork ``asyncio.run`` would bind that session to the
        first loop, then fail every later dork with "Event loop is closed"
        when the next ``asyncio.run`` spins up a fresh loop. Provider sessions
        are closed inside the same loop so nothing leaks a closed-loop
        connector (re-opening is lazy and safe).

        Lazy credit control: dorks run one at a time and the loop breaks as
        soon as MAX_PDFS *unseen* PDF URLs are collected — a pass stops
        spending search credits once it has enough PDFs to fetch (CLAUDE.md §4:
        discovery must not burn a provider budget it does not need).

        Phase G yield loop: with a ``yield_store`` present, a dork template
        already proven (MIN_TRIALS dispatches, zero working leads) is SKIPPED
        before its search — no credit spent on a dead dork — and every dork
        actually dispatched records a trial. The returned ``url_to_dork`` maps
        each surfaced PDF to the dork template that produced it for yield
        attribution.
        """
        from app.search_providers.manager import SearchProviderManager
        from app.search_providers.models import SearchQuery
        from app.search_providers.registry import get_registry

        manager = SearchProviderManager()
        seen: set[str] = set(skip or ())
        urls: list[str] = []
        url_to_dork: dict[str, str] = {}
        candidate_text: dict[str, str] = {}

        async def _run_all() -> None:
            try:
                for label, dork in dork_pairs:
                    if len(urls) >= MAX_PDFS:
                        break
                    # Learned-dork gate (Phase G + I): a template with MIN_TRIALS
                    # dispatches and zero working leads is dropped BEFORE its
                    # search is issued — the whole point is to stop spending a
                    # provider credit on it. Default-keep until enough data. The
                    # gate is SEGMENT-aware: a dork dead for this (trade,
                    # location) segment is skipped here while still dispatching
                    # where it earns working leads (global-drop still wins).
                    if self._yield_store is not None and self._yield_store.should_skip(label, segment):
                        logger.info(
                            "PlanHolderSource: dork template learned dead, "
                            "skipping dispatch: %r", label[:70],
                        )
                        continue
                    response = await manager.search(
                        SearchQuery(keywords=dork, num_results=RESULTS_PER_DORK)
                    )
                    # Dispatch credited at issue time, never backfilled (P-D);
                    # the trial lands on the (template, segment) row.
                    if self._yield_store is not None:
                        self._yield_store.record_dispatch(label, segment)
                    # Surface search provider failures immediately so the
                    # caller knows WHY zero PDFs were found (CLAUDE.md §5/§6).
                    # ``getattr`` keeps the injected-seam fakes (which expose
                    # only ``results``) working unchanged.
                    status = getattr(response, "status", "success")
                    if status == "error":
                        error = getattr(response, "error", "") or ""
                        if error:
                            self._last_search_errors.append(
                                f"search({dork[:60]}…): {error}"
                            )
                            logger.warning(
                                "PlanHolderSource: dork search failed: %s — %s",
                                dork,
                                error,
                            )
                    for result in response.results:
                        url = getattr(result, "url", None) or ""
                        if not url or not _is_pdf(url):
                            continue
                        if url in seen:
                            continue
                        seen.add(url)
                        urls.append(url)
                        url_to_dork[url] = label
                        candidate_text[url] = _result_text(result)
                        if len(urls) >= MAX_PDFS:
                            break
            finally:
                for provider in get_registry().get_enabled():
                    close = getattr(provider, "close", None)
                    if close:
                        try:
                            await close()
                        except Exception:  # noqa: BLE001 - closing is best-effort
                            pass

        asyncio.run(_run_all())
        self._last_candidate_text = candidate_text
        return urls, url_to_dork

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
            # "not fetched", not "unreachable": an oversize body is a refusal
            # by MAX_PDF_BYTES, and calling that unreachable would misreport a
            # document we deliberately declined as one we could not reach.
            logger.info("PlanHolderSource: %s not fetched — %s", url, exc)
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

        # stream=True is load-bearing: plain GET buffers the ENTIRE body before
        # returning, so by the time any size check could run the bytes are
        # already resident — the check would only be a post-mortem. Streaming
        # lets the declared length be refused before the body is read, and caps
        # an undeclared/lying length mid-read.
        resp = requests.get(
            url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True,
            stream=True,
        )
        try:
            resp.raise_for_status()
            try:
                declared = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared > MAX_PDF_BYTES:
                raise ValueError(
                    f"Content-Length {declared} is past MAX_PDF_BYTES "
                    f"{MAX_PDF_BYTES}"
                )
            body = resp.raw.read(MAX_PDF_BYTES + 1, decode_content=True)
        finally:
            resp.close()
        if len(body) > MAX_PDF_BYTES:
            raise ValueError(
                f"body read past MAX_PDF_BYTES {MAX_PDF_BYTES} with no usable "
                "Content-Length"
            )
        return body

    # -- row -> company record ---------------------------------------------

    def _to_company_record(self, row: PlanHolderRow, *, discovery_dork: str = "") -> dict[str, Any]:
        """Project one plan-holder row onto the source company schema.

        The connector's step-3 filters and ``_normalize_company`` adapter
        consume ``company_name``/``website``/``source_url``/``data_provenance``
        like any other source record (CLAUDE.md §4: every source replaceable).
        The plan-holder person/emails/phones/date ride in ``metadata["plan_holder"]``
        so a later increment (Inc 3) can bridge the pre-bound person into the
        LeadPipeline without re-crawling the site — and so nothing is lost here.

        ``discovery_dork`` (Phase G) is the dork TEMPLATE that surfaced this
        row's PDF, carried as ``_discovery_dork`` so the pipeline can attribute
        a future WORKING lead back to the dork that produced it. Empty for rows
        whose PDF had no attributable dork (e.g. the injected search seam).
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
            # Layer-1 yield attribution (Phase G): the dork TEMPLATE that
            # surfaced this company's PDF. "" when unattributable.
            "_discovery_dork": discovery_dork,
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
