"""Plugin source — presents the plugin framework as ONE discovery source.

This is the seam between Phase 2.1 (the plugin framework) and the
existing discovery pipeline. It exists so that
:class:`~app.discovery.source_orchestrator.SourceOrchestrator` remains
the single entry point of discovery and needs no knowledge of plugins
at all.

Why an adapter rather than teaching the orchestrator about plugins
-----------------------------------------------------------------
Phase 2.1 deliberately gave :class:`BaseDiscoveryPlugin` the *same*
call signature as :class:`~app.discovery.sources.base_source.BaseSource`::

    discover(*, industry, location, limit) -> (status, companies, metadata)

Because the shapes already match, the whole framework can be attached to
the pipeline by wrapping it — no orchestrator change, no second
execution loop, no duplicated aggregation logic (CLAUDE.md §14). The
orchestrator keeps owning cross-source deduplication, ranking, fixture
fallback and the ``data_source`` / ``bridge_mode`` decision; the
framework keeps owning per-plugin isolation, metrics and health.

One source for all plugins, not one source per plugin
-----------------------------------------------------
All plugins are executed through a single
:meth:`~app.discovery.plugins.plugin_manager.PluginManager.discover_all`
call. Registering each plugin directly with the orchestrator would work
mechanically but would bypass :class:`PluginManager` entirely — losing
the per-plugin metrics, health tracking and failure isolation that
Phase 2.1 exists to provide.

Optional by construction
------------------------
Nothing here runs unless a plugin has been registered. Use
:func:`attach_plugin_source`, which registers this source only when the
shared registry actually holds a plugin that would be selected, and logs
the decision either way. With an empty registry the pipeline is left
byte-for-byte as it was before Phase 2.2 (CLAUDE.md §5: never silent).

No provider dependency
----------------------
This module imports the framework and the source base class only. It
does not import — and must never import — any concrete provider.
"""

from __future__ import annotations

import logging
from typing import Any

from app.discovery.plugins.base_plugin import PluginCapability
from app.discovery.plugins.plugin_manager import (
    PluginManager,
    PluginRunResult,
    get_manager,
)
from app.discovery.sources.base_source import BaseSource
from app.discovery.sources.status import SourceStatus

logger = logging.getLogger(__name__)

#: Execution priority for the plugin source.
#:
#: Lower than ``SearchProviderSource`` (50) and far above
#: ``FixtureSource`` (999). Plugins are real discovery sources —
#: government registries, directories, direct website crawls — while
#: search APIs are optional infrastructure that must never define
#: discovery quality (CLAUDE.md §3, §8). So plugins run first.
DEFAULT_PLUGIN_SOURCE_PRIORITY: int = 40


class PluginSource(BaseSource):
    """Adapts the plugin framework to the :class:`BaseSource` contract.

    Executes every selected plugin through :class:`PluginManager` and
    reports the combined outcome as a single source result.

    Usage:
        orchestrator = SourceOrchestrator()
        attach_plugin_source(orchestrator)          # preferred
        orchestrator.register(SearchProviderSource())
        orchestrator.register(FixtureSource())

    Attributes:
        source_name: Identifier used in orchestrator diagnostics.
        priority: Execution order (lower runs first).
        enabled: Whether the orchestrator should call this source.
    """

    source_name = "discovery_plugins"
    description = "Discovery plugin framework (all registered plugins)"
    priority = DEFAULT_PLUGIN_SOURCE_PRIORITY
    enabled = True

    def __init__(
        self,
        *,
        manager: PluginManager | None = None,
        capability: PluginCapability | str | None = None,
        priority: int | None = None,
    ) -> None:
        """Initialize the plugin source.

        Args:
            manager: Manager to execute against. Defaults to the shared
                process-wide manager, so per-plugin metrics and health
                accumulate across requests instead of resetting each
                time the pipeline is built.
            capability: Restrict execution to plugins declaring this
                capability. ``None`` runs every enabled plugin. The
                pipeline stage that owns this source should set it
                explicitly rather than relying on a hidden default.
            priority: Override the execution priority.
        """
        self._manager = manager if manager is not None else get_manager()
        self._capability = capability
        if priority is not None:
            self.priority = priority

    @property
    def manager(self) -> PluginManager:
        """The manager this source executes against."""
        return self._manager

    @property
    def capability(self) -> PluginCapability | str | None:
        """The capability filter applied to plugin selection, if any."""
        return self._capability

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        """Run every selected plugin and combine their results.

        Results are returned raw — not deduplicated and not truncated to
        ``limit``. Cross-source deduplication and the final cap belong
        to the orchestrator, which already implements both; doing either
        here would create a second, competing implementation.

        Args:
            industry: Industry keyword (e.g. ``"Roofing"``).
            location: Geographic location (e.g. ``"Dallas Texas"``).
            limit: Maximum results requested per plugin.

        Returns:
            Tuple of ``(status, companies, metadata)``. Never raises.
        """
        try:
            results, diagnostics = self._manager.discover_all(
                industry=industry,
                location=location,
                limit=limit,
                capability=self._capability,
            )
        except Exception as exc:  # a source must never raise
            logger.exception(
                "PluginSource: PluginManager.discover_all raised %s",
                type(exc).__name__,
            )
            return SourceStatus.ERROR, [], {
                "source": self.source_name,
                "error": str(exc),
            }

        companies = self._collect_companies(results)
        status = self._aggregate_status(results, companies)

        metadata: dict[str, Any] = {
            "source": self.source_name,
            "status": status.value,
            "results_count": len(companies),
            "plugins_registered": diagnostics["plugins_registered"],
            "plugins_selected": diagnostics["plugins_selected"],
            "plugins_executed": diagnostics["plugins_executed"],
            "capability_filter": diagnostics["capability_filter"],
            "selection_reason": diagnostics["selection_reason"],
            "duplicates_removed": diagnostics["duplicates_removed"],
            "elapsed_ms": diagnostics["elapsed_ms"],
            "plugin_stats": diagnostics["plugin_stats"],
            "error": self._error_summary(results),
        }

        logger.info(
            "PluginSource [%s]: %d plugin(s) executed, %d company/companies "
            "(reason=%s)",
            status.value,
            diagnostics["plugins_executed"],
            len(companies),
            diagnostics["selection_reason"],
        )
        return status, companies, metadata

    @staticmethod
    def _collect_companies(
        results: list[PluginRunResult],
    ) -> list[dict[str, Any]]:
        """Gather companies from plugins that reported SUCCESS.

        Only SUCCESS contributes data. A plugin reporting EMPTY,
        UNAVAILABLE or ERROR alongside a non-empty list is a contract
        violation, so its data is dropped and the discrepancy logged
        rather than silently merged.
        """
        companies: list[dict[str, Any]] = []
        for result in results:
            if result.status == SourceStatus.SUCCESS:
                companies.extend(result.companies)
            elif result.companies:
                logger.warning(
                    "Plugin %s returned %d company/companies with "
                    "non-SUCCESS status %s; discarding them",
                    result.plugin_name,
                    len(result.companies),
                    result.status.value,
                )
        return companies

    @staticmethod
    def _aggregate_status(
        results: list[PluginRunResult],
        companies: list[dict[str, Any]],
    ) -> SourceStatus:
        """Reduce per-plugin statuses to one source-level status.

        SUCCESS requires actual companies. A plugin that reports SUCCESS
        with an empty list must not make this source look successful:
        the orchestrator treats any non-fixture SUCCESS as proof that
        live discovery worked, and would then set ``data_source="live"``
        while returning nothing and skipping the fixture bridge. That is
        exactly the kind of fake "live" mode CLAUDE.md §12 forbids.
        """
        if companies:
            return SourceStatus.SUCCESS
        if not results:
            # Nothing ran. PluginManager has already logged why at ERROR
            # level; EMPTY lets the orchestrator continue to the next
            # source, which is the pre-Phase-2.2 behaviour.
            return SourceStatus.EMPTY

        statuses = {r.status for r in results}
        if SourceStatus.SUCCESS in statuses:
            logger.warning(
                "PluginSource: a plugin reported SUCCESS but contributed no "
                "companies; reporting EMPTY so the pipeline does not treat "
                "this as a successful live result"
            )
            return SourceStatus.EMPTY
        if SourceStatus.ERROR in statuses:
            return SourceStatus.ERROR
        if SourceStatus.UNAVAILABLE in statuses:
            return SourceStatus.UNAVAILABLE
        return SourceStatus.EMPTY

    @staticmethod
    def _error_summary(results: list[PluginRunResult]) -> str:
        """One-line summary of every plugin that failed, or ""."""
        failures = [
            f"{r.plugin_name}: {r.error or r.status.value}"
            for r in results
            if r.status in (SourceStatus.ERROR, SourceStatus.UNAVAILABLE)
        ]
        return "; ".join(failures)

    async def health_check(self) -> dict[str, Any]:
        """Probe every registered plugin and summarize the result.

        Mirrors
        :class:`~app.discovery.sources.search_provider_source.SearchProviderSource`:
        a source with nothing behind it reports unhealthy, because it
        cannot produce data.

        Returns:
            Dict with ``healthy``, ``source``, plugin counts, and the
            full per-plugin health report.
        """
        health = await self._manager.check_health()
        operational = [n for n, h in health.items() if h.is_operational]
        return {
            "healthy": bool(operational),
            "source": self.source_name,
            "plugins_registered": len(self._manager.registry),
            "plugins_operational": len(operational),
            "plugin_names": self._manager.registry.names(),
            "plugin_health": {n: h.to_dict() for n, h in health.items()},
        }

    def __repr__(self) -> str:
        return (
            f"<PluginSource name={self.source_name!r}"
            f" priority={self.priority}"
            f" enabled={self.enabled}"
            f" plugins={len(self._manager.registry)}"
            f" capability={self._capability}>"
        )


def attach_plugin_source(
    orchestrator: Any,
    *,
    manager: PluginManager | None = None,
    capability: PluginCapability | str | None = None,
    priority: int | None = None,
) -> PluginSource | None:
    """Register a :class:`PluginSource` with an orchestrator, if useful.

    This is the supported way to wire the plugin framework into the
    pipeline. It makes plugin execution genuinely optional: when no
    plugin would be selected, nothing is registered, so the orchestrator
    sees exactly the sources it saw before Phase 2.2 — identical
    ``source_stats``, identical ``fallback_reason``, identical
    ``sources_executed``.

    The decision is logged at INFO either way, so "no plugins ran" is
    never silent (CLAUDE.md §5, §6).

    Args:
        orchestrator: The :class:`SourceOrchestrator` to register with.
        manager: Manager to execute against. Defaults to the shared one.
        capability: Restrict to plugins declaring this capability.
            Callers should pass the capability their pipeline stage
            actually needs, so an unrelated plugin cannot leak results
            into the wrong stage.
        priority: Override the source's execution priority.

    Returns:
        The registered :class:`PluginSource`, or ``None`` when no plugin
        would have been selected and nothing was registered.
    """
    manager = manager if manager is not None else get_manager()
    registry = manager.registry

    if capability is None:
        selectable = registry.get_enabled()
    else:
        selectable = registry.get_by_capability(capability)

    if not selectable:
        logger.info(
            "PluginSource NOT attached: no plugin would be selected "
            "(%d registered, %d enabled, capability=%s). The discovery "
            "pipeline runs unchanged. Register plugins via "
            "app.discovery.plugins.get_registry().register(...).",
            len(registry),
            len(registry.get_enabled()),
            capability if capability is not None else "any",
        )
        return None

    source = PluginSource(
        manager=manager,
        capability=capability,
        priority=priority,
    )
    orchestrator.register(source)
    logger.info(
        "PluginSource attached with %d selectable plugin(s): %s "
        "(capability=%s, priority=%d)",
        len(selectable),
        [p.name for p in selectable],
        capability if capability is not None else "any",
        source.priority,
    )
    return source
