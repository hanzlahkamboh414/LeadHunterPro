"""Source Orchestrator — multi-source discovery pipeline.

Coordinates execution across multiple independent discovery sources:
- Texas Secretary of State business search
- Texas CMBL vendor registry
- Web search providers (SearXNG, Brave)
- County procurement portals
- Trade directories (AGC, BBB)
- Licensing databases
- Fixture bridge (emergency fallback)

Sources are executed in priority order (lower = higher priority).
Results from all sources are aggregated, deduplicated, and returned.
If ALL live sources fail, the fixture bridge activates with clear logging.

Status contract per source:
  SUCCESS   — source returned companies
  EMPTY     — source ran but found nothing
  UNAVAILABLE — network/DNS failure (source cannot reach target)
  ERROR     — unexpected exception during execution

The orchestrator NEVER treats UNAVAILABLE or ERROR as terminal.
It continues with the next source and reports per-source diagnostics.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.discovery.sources.status import SourceStatus

logger = logging.getLogger(__name__)


def _source_label(name: str, stats: dict[str, Any]) -> str:
    """One human-readable ``name: STATUS (detail)`` line for fallback reasons.

    Handles ``SourceStatus`` enums and plain strings so callers never crash
    on either shape.  The ``detail`` is the most specific fact the source
    returned: ``metadata.reason`` (e.g. ``search_failed``) when present, else
    the ``error`` string — never empty parens when there IS a fact to show.
    """
    status = stats.get("status", "unknown")
    status_str = status.value if hasattr(status, "value") else str(status)
    detail = (stats.get("metadata") or {}).get("reason") or stats.get("error") or ""
    return f"{name}: {status_str}" + (f" ({detail})" if detail else "")


def _normalize_status(status: Any) -> str:
    """Extract ``status.value`` from a ``SourceStatus`` enum, or ``str(status)``."""
    return status.value if hasattr(status, "value") else str(status)


class SourceOrchestrator:
    """Orchestrates discovery across multiple independent sources.

    Sources are registered via :meth:`register` and executed in priority
    order. The orchestrator aggregates results and falls back to the
    fixture source when no live source returns data.

    Usage:
        orchestrator = SourceOrchestrator()
        orchestrator.register(SOSBusinessSource())
        orchestrator.register(CMBLSource())
        orchestrator.register(SearchProviderSource())
        orchestrator.register(FixtureSource())
        companies, metadata = orchestrator.discover(
            industry="Roofing", location="Dallas Texas", limit=20
        )
        orchestrator.print_health_report(companies, metadata)
    """

    def __init__(self) -> None:
        """Initialize the orchestrator."""
        self._sources: list[Any] = []

    def register(self, source: Any) -> None:
        """Register a discovery source.

        Sources are sorted by priority (lower number = tried first).

        Args:
            source: An object implementing :class:`BaseSource`.
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

        Each source is called independently. If a source raises an
        exception or returns UNAVAILABLE/ERROR, the orchestrator logs
        the issue and moves to the next source — discovery never stops.

        Sources are executed in priority order. Results are aggregated
        and deduplicated by (domain, name) tuple. If no live source
        returns results, the fixture bridge activates.

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

        for source in self._sources:
            if not getattr(source, "enabled", True):
                logger.debug("Skipping disabled source: %s", source.source_name)
                continue

            source_name = getattr(source, "source_name", "unknown")
            try:
                status, companies, meta = source.discover(
                    industry=industry,
                    location=location,
                    limit=limit,
                )
                source_stats[source_name] = {
                    "status": _normalize_status(status),
                    "results": len(companies),
                    "metadata": meta,
                }
                logger.info(
                    "Source %s [%s]: %d results",
                    source_name,
                    status.value,
                    len(companies),
                )
                if status == SourceStatus.SUCCESS:
                    for company in companies:
                        # Tag each record with the source that produced it so a
                        # downstream consumer that filters the aggregate (e.g. a
                        # connector's state/city/industry filter) can still label
                        # data_source from what actually survives, not from raw
                        # per-source statuses (CLAUDE.md §1).
                        company.setdefault("_discovery_source", source_name)
                    all_companies.extend(companies)

            except Exception as exc:  # noqa: BLE001
                source_stats[source_name] = {
                    "status": SourceStatus.ERROR,
                    "results": 0,
                    "error": str(exc),
                }
                logger.error(
                    "Source %s failed: %s",
                    source_name,
                    exc,
                    exc_info=True,
                )

        elapsed_ms = (time.monotonic() - start_time) * 1000

        # Deduplicate by (domain, normalized_name) tuple
        deduped = self._deduplicate(all_companies)

        # Determine overall data source status
        live_success = [
            name
            for name, stats in source_stats.items()
            if stats.get("status") == SourceStatus.SUCCESS
            and name != "fixture_bridge"
        ]
        if live_success:
            data_source = "live"
            fallback_reason = ""
        elif any(
            getattr(s, "source_name") == "fixture_bridge"
            and getattr(s, "enabled", True)
            for s in self._sources
        ):
            data_source = "fixture"
            parts = [
                _source_label(k, v)
                for k, v in source_stats.items()
                if k != "fixture_bridge"
            ]
            fallback_reason = "; ".join(parts) or "no_live_providers_configured"
        else:
            data_source = "empty"
            # Honest per-source breakdown (CLAUDE.md §5/§6): when no fixture
            # bridge is registered the only fallback is the truth about what
            # each source returned.  ``no_sources_registered`` was misleading
            # when providers WERE registered but every one of them errored or
            # returned empty — callers need the per-source status to diagnose.
            parts = [
                _source_label(k, v)
                for k, v in source_stats.items()
            ]
            fallback_reason = "; ".join(parts) or "no_sources_configured"

        metadata: dict[str, Any] = {
            "data_source": data_source,
            "bridge_mode": data_source == "fixture",
            "fallback_reason": fallback_reason,
            "total_raw": len(all_companies),
            "total_deduped": len(deduped),
            "total_returned": min(len(deduped), limit),
            "elapsed_ms": round(elapsed_ms, 1),
            "source_stats": source_stats,
            "sources_executed": len([s for s in self._sources if getattr(s, "enabled", True)]),
            # Backwards compat: errors dict for tests that assert its presence.
            # In the new status contract, errors live inside source_stats[<name>]["status"].
            "errors": {
                name: stats.get("error", "")
                for name, stats in source_stats.items()
                if stats.get("error")
            },
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

    def print_health_report(
        self,
        companies: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> None:
        """Print a human-readable execution summary.

        Usage:
            companies, meta = orchestrator.discover(...)
            orchestrator.print_health_report(companies, meta)

        Output format:
            Source Health
            --------------
            sos_business           SUCCESS      (12 companies)
            cmbL_source            UNAVAILABLE  (0 companies)
            search_provider        DISABLED     (0 companies)
            fixture_bridge         NOT_USED     (0 companies)
            ────────────────────────────────────────────
            Final                  12 companies | source=live | 234.7ms
        """
        source_stats = metadata.get("source_stats", {})
        max_name_len = max((len(name) for name in source_stats), default=0)
        max_name_len = max(max_name_len, 12)  # minimum column width

        lines = []
        lines.append("")
        lines.append("Source Health")
        lines.append("-" * 40)

        for source in self._sources:
            # `name` must be bound before the branch: reading it only in the
            # enabled branch raised NameError when the first source was
            # disabled, and printed the *previous* source's name when a later
            # one was. Both are reachable now that a source can be disabled
            # to make plugin execution optional.
            name = getattr(source, "source_name", "unknown")
            if not getattr(source, "enabled", True):
                status_str = "DISABLED".ljust(12)
                count_str = "0 companies"
            else:
                stats = source_stats.get(name, {})
                status = stats.get("status", "empty")
                count = stats.get("results", 0)
                status_str = _normalize_status(status).upper().ljust(12)
                count_str = f"{count} companies"
            lines.append(f"  {name:<{max_name_len}}  {status_str}  ({count_str})")

        lines.append("  " + "-" * (max_name_len + 30))
        total = metadata["total_returned"]
        source_type = metadata["data_source"]
        elapsed = metadata["elapsed_ms"]
        lines.append(
            f"  {'Final':<{max_name_len}}  {total} companies "
            f"| source={source_type} | {elapsed:.1f}ms"
        )
        if metadata.get("fallback_reason"):
            lines.append(f"  {'Fallback':<{max_name_len}}  {metadata['fallback_reason']}")
        lines.append("")
        print("\n".join(lines))

    @staticmethod
    def _deduplicate(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove duplicate companies by BOTH domain AND normalized name.

        A company is considered a duplicate only when BOTH its domain
        AND its name match a previously-seen company. This prevents
        accidentally dropping genuinely different businesses that
        happen to share a name (e.g. "Acme Roofing" in Dallas vs Houston).

        Duplicates are MERGED, not silently discarded (Phase 2 Step 4, F):
        the first-seen record is kept as the primary and the duplicate's
        evidence + provenance are folded into it, so no source's evidence is
        lost and conflicting location is recorded explicitly rather than
        first-wins. The ``(domain, name)`` key is unchanged.

        Args:
            companies: Raw company dicts from all sources.

        Returns:
            Deduplicated list preserving first-seen order.
        """
        from urllib.parse import urlparse

        from app.engines.verification.acceptance_gate import merge_company_records

        seen: dict[tuple[str, str], int] = {}
        unique: list[dict[str, Any]] = []

        for company in companies:
            url = company.get("website", "").strip()
            parsed = urlparse(url)
            domain = (parsed.hostname or "").lower().replace("www.", "").strip()
            name = (company.get("company_name", "") or "").strip().lower()

            key = (domain, name)
            if key in seen:
                idx = seen[key]
                unique[idx] = merge_company_records(unique[idx], company)
                logger.debug(
                    "Deduplicated (evidence merged): %s (%s / %s)",
                    company.get("company_name", "?"),
                    domain,
                    name,
                )
                continue

            seen[key] = len(unique)
            unique.append(company)

        return unique
