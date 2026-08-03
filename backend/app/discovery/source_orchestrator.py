"""Source Orchestrator — multi-source discovery pipeline.

Coordinates execution across multiple independent discovery sources:
- Web search providers (SearXNG, Brave)
- Public procurement portals
- Trade directories (AGC, BBB)
- Licensing databases
- Fixture bridge (emergency fallback)

Sources are executed in priority order (lower = higher priority).
Results from all sources are aggregated, deduplicated, and returned.
If ALL live sources fail, the fixture bridge activates with clear logging.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class SourceOrchestrator:
    """Orchestrates discovery across multiple independent sources.

    Sources are registered via :meth:`register` and executed in priority
    order. The orchestrator aggregates results and falls back to the
    fixture source when no live source returns data.

    Usage:
        orchestrator = SourceOrchestrator()
        orchestrator.register(SearchProviderSource())
        orchestrator.register(FixtureSource())
        companies, metadata = orchestrator.discover(
            industry="Roofing", location="Dallas Texas", limit=20
        )
    """

    def __init__(self) -> None:
        """Initialize the orchestrator."""
        self._sources: list[Any] = []

    def register(self, source: Any) -> None:
        """Register a discovery source.

        Sources are sorted by priority (lower number = tried first).

        Args:
            source: An object with a ``discover()`` method returning
                ``(list[dict], dict)``. Must have ``source_name``,
                ``priority``, and ``enabled`` attributes.
        """
        if source in self._sources:
            logger.warning(
                "Source '%s' already registered, skipping", source.source_name
            )
            return
        self._sources.append(source)
        self._sources.sort(key=lambda s: getattr(s, "priority", 999))
        logger.info(
            "Registered source: %s (priority=%d, enabled=%s)",
            source.source_name,
            getattr(source, "priority", 999),
            getattr(source, "enabled", True),
        )

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Execute discovery across all registered sources.

        Sources are executed in priority order. Results are aggregated
        and deduplicated by company name + domain. If no live source
        returns results, the fixture bridge is activated.

        Args:
            industry: Industry keyword (e.g. "Roofing").
            location: Geographic location (e.g. "Dallas Texas").
            limit: Maximum total results to return.

        Returns:
            Tuple of (company dicts, metadata dict).
        """
        start_time = time.monotonic()

        logger.info(
            "SourceOrchestrator.discover: industry=%r location=%r limit=%d",
            industry,
            location,
            limit,
        )
        logger.info(
            "SourceOrchestrator: %d sources registered",
            len(self._sources),
        )

        all_companies: list[dict[str, Any]] = []
        source_stats: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}

        for source in self._sources:
            if not getattr(source, "enabled", True):
                logger.debug("Skipping disabled source: %s", source.source_name)
                continue

            source_name = getattr(source, "source_name", "unknown")
            try:
                companies, meta = source.discover(
                    industry=industry,
                    location=location,
                    limit=limit,
                )
                source_stats[source_name] = {
                    "results": len(companies),
                    "metadata": meta,
                }
                logger.info(
                    "Source %s: %d results",
                    source_name,
                    len(companies),
                )
                all_companies.extend(companies)

            except Exception as exc:
                errors[source_name] = str(exc)
                source_stats[source_name] = {"error": str(exc)}
                logger.exception("Source %s failed", source_name)

        elapsed_ms = (time.monotonic() - start_time) * 1000

        # Deduplicate by normalized domain
        deduped = self._deduplicate(all_companies)

        # Determine data source status
        live_sources = [
            name
            for name in source_stats
            if name != "fixture_bridge" and source_stats[name].get("results", 0) > 0
        ]
        if live_sources:
            data_source = "live"
            fallback_reason = ""
        elif self._sources and any(
            s.source_name == "fixture_bridge" for s in self._sources
        ):
            data_source = "fixture"
            fallback_reason = (
                "all_live_sources_failed_or_unavailable: "
                + "; ".join(
                    f"{k}: {v.get('error', 'no_results')}"
                    for k, v in source_stats.items()
                    if k != "fixture_bridge"
                )
                or "no_live_providers_configured"
            )
        else:
            data_source = "empty"
            fallback_reason = "no_sources_registered"

        metadata: dict[str, Any] = {
            "data_source": data_source,
            "fallback_reason": fallback_reason,
            "total_raw": len(all_companies),
            "total_deduped": len(deduped),
            "total_returned": min(len(deduped), limit),
            "elapsed_ms": round(elapsed_ms, 1),
            "source_stats": source_stats,
            "errors": errors,
            "sources_executed": len(
                [s for s in self._sources if getattr(s, "enabled", True)]
            ),
        }

        logger.info(
            "SourceOrchestrator.complete: %d raw -> %d deduped -> %d returned "
            "(%.1fms, source=%s)",
            len(all_companies),
            len(deduped),
            min(len(deduped), limit),
            elapsed_ms,
            data_source,
        )

        return deduped[:limit], metadata

    @staticmethod
    def _deduplicate(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove duplicate companies by BOTH domain AND normalized name.

        A company is considered a duplicate only when BOTH its domain
        AND its name match a previously-seen company. This prevents
        accidentally dropping genuinely different businesses that
        happen to share a name (e.g. "Acme Roofing" in Dallas vs Houston).

        Args:
            companies: Raw company dicts from all sources.

        Returns:
            Deduplicated list preserving first-seen order.
        """
        from urllib.parse import urlparse

        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []

        for company in companies:
            url = company.get("website", "").strip()
            parsed = urlparse(url)
            domain = (parsed.hostname or "").lower().replace("www.", "").strip()
            name = (company.get("company_name", "") or "").strip().lower()

            key = (domain, name)
            if key in seen:
                logger.debug(
                    "Deduplicated: %s (%s / %s)",
                    company.get("company_name", "?"),
                    domain,
                    name,
                )
                continue

            seen.add(key)
            unique.append(company)

        return unique
