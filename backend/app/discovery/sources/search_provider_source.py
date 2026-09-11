"""Search provider source — wraps SearchProviderManager as a BaseSource.

Allows SearXNG and Brave Search to be used as ONE source among many
in the SourceOrchestrator pipeline. If no providers are configured,
returns empty results gracefully (does not crash).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.discovery.sources.base_source import BaseSource
from app.discovery.sources.status import SourceStatus
from app.search_providers.manager import SearchProviderManager
from app.search_providers.models import SearchQuery, SearchResponse

logger = logging.getLogger(__name__)


def _dedup_query_text(industry: str, city: str, state: str) -> str:
    """Build the provider keyword string WITHOUT repeating the trade word.

    ``industry`` already names the trade ('general contractor'), so
    ``f"{industry} contractor {city} {state}"`` emitted 'general contractor
    contractor Dallas TX' — a noisier provider query AND a contaminated
    training signal for query-yield learning. Only append the bare role word
    when the industry string does not already contain it (a repeated token
    adds nothing to a search).
    """
    loc = " ".join(p for p in (city, state) if p)
    if "contractor" in industry.lower():
        return " ".join(p for p in (industry, loc) if p)
    return " ".join(p for p in (industry, "contractor", loc) if p)


async def _search_then_close(
    manager: SearchProviderManager,
    query: SearchQuery,
    providers: list[Any],
) -> SearchResponse:
    """Run one search and close provider sessions in the SAME event loop.

    An unbounded long-lived backend that never closed sessions would leak one
    ``aiohttp`` session per pass (repeated ``asyncio.run`` in this source) and
    eventually exhaust file descriptors — at which point NEW connections fail
    and a transient provider outage becomes a permanent 0-result run. The
    providers' sessions are loop-bound, so ``close()`` must happen inside this
    loop, never in a second ``asyncio.run``.
    """
    try:
        return await manager.search(query)
    finally:
        for provider in providers:
            close = getattr(provider, "close", None)
            if close:
                try:
                    await close()
                except Exception:  # noqa: BLE001 — closing is best-effort
                    pass


def _to_companies(response: Any, dork: str = "") -> list[dict[str, Any]]:
    """Convert one provider response to company dicts for downstream use.

    ``dork`` (Inc 2) stamps the producing web-angle template on every record
    (``_discovery_dork``) so a WORKING lead is credited back to its angle in
    the Phase G yield loop — empty for the default keyword query.
    """
    companies: list[dict[str, Any]] = []
    for result in response.results:
        companies.append(
            {
                "company_name": result.title,
                "website": result.url,
                # ACCURACY-FIRST (Phase 2 Step 3): the query is search
                # intent, never company location evidence. A search
                # result carries NO city/state unless the result itself
                # proves one — empty means unknown, not filled from query.
                "city": "",
                "state": "",
                "country": "USA",
                "trade_category": "",  # Classified downstream
                "industry_focus": result.snippet or result.title,
                "revenue_tier": "",
                "source_url": result.url,
                "data_provenance": f"live:{result.position}",
                # Additive attribution (Step 0.5): the producing provider,
                # stamped by the manager before merge. data_provenance keeps
                # its old "live:{position}" contract — provider identity is
                # a SEPARATE field so no consumer of the provenance string
                # changes (P-H guardrail).
                "provider_name": result.provider or "",
                "discovery_reason": "",
                # Inc 2 — web-angle attribution (Phase G yield loop).
                "_discovery_dork": dork,
            }
        )
    return companies


class SearchProviderSource(BaseSource):
    """Wraps SearchProviderManager as a SourceOrchestrator-compatible source.

    This source attempts live web search via configured providers
    (SearXNG, Brave). Falls back to empty results if no providers
    are registered or all fail.

    Inc 2 — web-angle lane: when a ``candidate_store``/``yield_store`` pair is
    injected, each discover() call ALSO runs ONE LLM-invented web angle
    (layer-``'web'`` template: member directories, license rosters, bid
    boards…) alongside the default keyword query, rotating through the
    not-yet-proven-dead angles. Records from an angle query carry the angle
    label in ``_discovery_dork`` so the SAME Phase G earn-or-die yield loop
    credits working leads back to (or kills) the angle — a proposal is never
    injected as proven (§12).
    """

    source_name = "search_providers"
    description = "Web search via SearXNG / Brave API (optional)"
    priority = 50
    enabled = True

    #: Inc 2 — the rotation cursor is CLASS-level: the pipeline re-instantiates
    #: this source for EVERY discovery pass, so an instance cursor resets each
    #: time and dispatches angle[0] forever (live proof 2026-09-11: 6/6 passes
    #: probed only 'state board lookup'). Class-level state survives across
    #: instances within the process, so consecutive passes probe DIFFERENT
    #: angles. Discovery passes are produced by ONE thread, so an int cursor
    #: needs no lock.
    _angle_cursor = 0

    def __init__(self, candidate_store: Any | None = None,
                 yield_store: Any | None = None) -> None:
        """Initialize the search provider source.

        ``candidate_store``/``yield_store`` are optional (the Layer-2 web-angle
        lane is off without them — exact pre-Inc-2 behavior).
        """
        self._manager: SearchProviderManager | None = None
        self._candidate_store = candidate_store
        self._yield_store = yield_store

    def _ensure_manager(self) -> None:
        """Lazy-initialize the SearchProviderManager."""
        if self._manager is None:
            self._manager = SearchProviderManager()

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Execute search via registered providers.

        Args:
            industry: Industry keyword.
            location: Geographic location.
            limit: Maximum results.

        Returns:
            Tuple of (company dicts, metadata).
            Returns ([], {...}) on any failure — never raises.
        """
        self._ensure_manager()

        from app.search_providers.registry import get_registry

        registry = get_registry()
        enabled = registry.get_enabled()

        if not enabled:
            logger.debug(
                "SearchProviderSource: no providers registered "
                "(SEARXNG_URL and BRAVE_SEARCH_API_KEY not set)"
            )
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "error": "no_providers_configured",
                "providers_available": [],
            }

        from app.connectors.texas_procurement import _parse_location

        city, state = _parse_location(location)
        query_text = _dedup_query_text(industry, city, state)

        logger.info(
            "SearchProviderSource: searching %r with %d providers",
            query_text,
            len(enabled),
        )

        response = self._search_once(query_text, limit, enabled)
        if response is None:
            return SourceStatus.ERROR, [], {
                "source": self.source_name,
                "error": "search exception",
                "providers_tried": [p.provider_name for p in enabled],
            }

        logger.info(
            "SearchProviderSource: status=%r results=%d error=%r",
            response.status,
            len(response.results),
            response.error[:100] if response.error else "",
        )

        companies: list[dict[str, Any]] = _to_companies(response)

        # ------------------------------------------------------------------
        # Inc 2 — web-angle lane: ONE LLM-invented angle query per call,
        # rotated, stamped for the Phase G yield loop. A failure here never
        # breaks the default lane (best-effort, honestly logged).
        # ------------------------------------------------------------------
        angle_meta: dict[str, Any] = {}
        angle = self._next_web_angle(industry, location)
        if angle:
            angle_text = (
                angle.replace("{industry}", industry)
                     .replace("{location}", location)
            )
            logger.info(
                "SearchProviderSource: web angle %r -> %r",
                angle, angle_text,
            )
            angle_response = self._search_once(angle_text, limit, enabled)
            if angle_response is not None:
                logger.info(
                    "SearchProviderSource: web angle status=%r results=%d",
                    angle_response.status, len(angle_response.results),
                )
                angle_companies = _to_companies(angle_response, dork=angle)
                if angle_companies:
                    companies.extend(angle_companies)
                    angle_meta = {
                        "web_angle": angle,
                        "web_angle_results": len(angle_companies),
                    }

        if not companies:
            reason = response.error or "no_results"
            logger.debug("SearchProviderSource: no results — %s", reason)
            return SourceStatus.EMPTY, [], {
                "source": self.source_name,
                "status": response.status,
                "error": reason,
                "provider": response.provider,
            }

        return SourceStatus.SUCCESS, companies, {
            "source": self.source_name,
            "status": response.status,
            "results_count": len(companies),
            "provider": response.provider,
            "latency_ms": response.latency_ms,
            "error": response.error or "",
            **angle_meta,
        }

    def _search_once(self, query_text: str, limit: int,
                     enabled: list[Any]) -> Any | None:
        """One bounded provider search; None on exception (never raises)."""
        query = SearchQuery(keywords=query_text, num_results=limit * 3)
        try:
            # Search + session close run in ONE event loop: aiohttp sessions
            # are loop-bound, and closing with a second asyncio.run would fail.
            return asyncio.run(_search_then_close(self._manager, query, enabled))
        except Exception as exc:  # noqa: BLE001
            logger.error("SearchProviderSource: search exception: %s", exc)
            return None

    def _next_web_angle(self, industry: str, location: str) -> str:
        """The next not-yet-proven-dead web angle for this pass ('' = off).

        Rotation: one angle per discover() call, round-robin over the effective
        set, so consecutive passes probe DIFFERENT angles (bounded credit burn
        per pass, full coverage over a sweep). The cursor is CLASS-level — see
        ``_angle_cursor`` — because the pipeline rebuilds this source per pass.
        Reads the yield gate through
        ``candidate_store.effective_dorks(yield_store, segment, layer='web')``
        — the SAME earn-or-die contract as the plan-holder dorks.
        """
        if self._candidate_store is None or self._yield_store is None:
            return ""
        try:
            from app.discovery.yield_learning import segment_key

            angles = self._candidate_store.effective_dorks(
                self._yield_store,
                segment=segment_key(industry, location),
                layer="web",
            )
        except Exception:  # noqa: BLE001 — a broken store must not break search
            logger.debug("web-angle dispatch: store unavailable", exc_info=True)
            return ""
        if not angles:
            return ""
        angle = angles[SearchProviderSource._angle_cursor % len(angles)]
        SearchProviderSource._angle_cursor += 1
        return angle

    async def health_check(self) -> dict[str, Any]:
        """Check if any search providers are registered and healthy."""
        from app.search_providers.registry import get_registry

        registry = get_registry()
        enabled = registry.get_enabled()
        return {
            "healthy": len(enabled) > 0,
            "source": self.source_name,
            "providers_registered": len(enabled),
            "provider_names": [p.provider_name for p in enabled],
        }
