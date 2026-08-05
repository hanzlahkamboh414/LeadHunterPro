"""Plugin manager — executes registered plugins and records what happened.

The manager is the only component that calls a plugin. It owns three
responsibilities and no others:

1. Run enabled plugins, in priority order, isolating each one so that a
   single failing plugin can never stop discovery.
2. Convert every outcome — including an uncaught exception — into a
   :class:`SourceStatus`, so the layer above always gets the same shape.
3. Update per-plugin :class:`PluginMetrics` and :class:`PluginHealth`.

Explicitly **not** the manager's job:

- Deduplication
- Ranking
- Fixture / bridge fallback
- Deciding ``data_source`` or ``bridge_mode``

Those belong to
:class:`~app.discovery.source_orchestrator.SourceOrchestrator`, which
already implements them and is untouched by this phase. Duplicating
that logic here would create two competing pipelines and violate
CLAUDE.md §14. The manager returns raw per-plugin results and lets the
orchestrating layer decide.

On ``duplicates_removed``: because the manager does not deduplicate, it
cannot compute this number. It *records* what a plugin reports in
``metadata["duplicates_removed"]`` — duplicates the plugin filtered from
its own result set. Cross-plugin duplicates remain the orchestrator's
concern, and this counter deliberately does not attempt to reflect them.

By default the manager operates on the shared registry from
:func:`~app.discovery.plugins.plugin_registry.get_registry`, so plugins
registered anywhere in the application are visible here. Passing an
explicit registry is supported for isolated use such as tests.

Application code should reach the manager through :func:`get_manager`,
which returns one shared instance. Because metrics and health accumulate
across runs, a per-request manager would reset them on every search; see
:func:`get_manager` for the full rationale.

Logging follows CLAUDE.md §6: plugins found, plugins selected, query,
per-plugin status, results returned, and the reason when nothing ran.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.discovery.plugins.base_plugin import (
    BaseDiscoveryPlugin,
    PluginCapability,
    normalize_capability,
)
from app.discovery.plugins.plugin_health import PluginHealth
from app.discovery.plugins.plugin_metrics import PluginMetrics
from app.discovery.plugins.plugin_registry import PluginRegistry, get_registry
from app.discovery.sources.status import SourceStatus

logger = logging.getLogger(__name__)


@dataclass
class PluginRunResult:
    """Outcome of a single plugin execution.

    Attributes:
        plugin_name: Which plugin produced this result.
        status: The plugin's reported status.
        companies: Raw company dicts, exactly as the plugin returned
            them — not deduplicated, not ranked, not filtered.
        metadata: Plugin-supplied diagnostics.
        latency_ms: Wall-clock duration of the call.
        error: Exception text when the plugin raised, else "".
        duplicates_removed: Duplicates the plugin reported filtering
            from its own result set.
    """

    plugin_name: str
    status: SourceStatus
    companies: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    error: str = ""
    duplicates_removed: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize for diagnostics and API output."""
        return {
            "plugin_name": self.plugin_name,
            "status": self.status.value,
            "results": len(self.companies),
            "metadata": self.metadata,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
            "duplicates_removed": self.duplicates_removed,
        }


class PluginManager:
    """Executes discovery plugins and tracks their metrics and health.

    Usage:
        manager = PluginManager()          # uses the shared registry
        manager.register(TexasSOSPlugin())
        results, diagnostics = manager.discover_all(
            industry="Roofing", location="Dallas Texas", limit=20
        )
    """

    def __init__(self, registry: PluginRegistry | None = None) -> None:
        """Initialize the manager.

        Args:
            registry: Registry to execute against. Defaults to the
                process-wide shared registry, so plugins registered
                anywhere in the application are visible. Pass an
                explicit instance for isolated use such as tests.
        """
        self._registry = registry if registry is not None else get_registry()
        self._metrics: dict[str, PluginMetrics] = {}
        self._health: dict[str, PluginHealth] = {}

        # Adopt any plugins already in the registry, so their
        # metrics/health records exist before the first run.
        for plugin in self._registry.get_all():
            self._ensure_records(plugin.name)

    @property
    def registry(self) -> PluginRegistry:
        """The registry this manager executes against."""
        return self._registry

    # ------------------------------------------------------------------
    # Registration (delegates to the registry, adds tracking records)
    # ------------------------------------------------------------------

    def register(
        self,
        plugin: BaseDiscoveryPlugin,
        *,
        replace: bool = False,
    ) -> bool:
        """Register a plugin and create its metrics and health records.

        Args:
            plugin: The plugin to register.
            replace: Overwrite an existing plugin of the same name
                instead of skipping the duplicate.

        Returns:
            True if the plugin was registered (or replaced), False if a
            duplicate was skipped.
        """
        registered = self._registry.register(plugin, replace=replace)
        if registered:
            self._ensure_records(plugin.name)
        return registered

    def unregister(self, name: str) -> bool:
        """Unregister a plugin and drop its metrics and health records.

        Returns:
            True if a plugin was removed, False if the name was unknown.
        """
        removed = self._registry.unregister(name)
        if removed:
            self._metrics.pop(name, None)
            self._health.pop(name, None)
        return removed

    def _ensure_records(self, name: str) -> None:
        """Create metrics and health records for a plugin if absent."""
        self._metrics.setdefault(name, PluginMetrics(plugin_name=name))
        self._health.setdefault(name, PluginHealth(plugin_name=name))

    # ------------------------------------------------------------------
    # Metrics and health accessors
    # ------------------------------------------------------------------

    def get_metrics(self, name: str) -> PluginMetrics | None:
        """Return metrics for one plugin, or None if it is unknown."""
        return self._metrics.get(name)

    def get_all_metrics(self) -> dict[str, PluginMetrics]:
        """Return metrics for every tracked plugin, keyed by name."""
        return dict(self._metrics)

    def get_health(self, name: str) -> PluginHealth | None:
        """Return health for one plugin, or None if it is unknown."""
        return self._health.get(name)

    def get_all_health(self) -> dict[str, PluginHealth]:
        """Return health for every tracked plugin, keyed by name."""
        return dict(self._health)

    def reset_metrics(self) -> None:
        """Zero every plugin's counters. Health is left untouched."""
        for metrics in self._metrics.values():
            metrics.reset()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def discover_all(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
        capability: PluginCapability | str | None = None,
    ) -> tuple[list[PluginRunResult], dict[str, Any]]:
        """Run every enabled plugin and collect their results.

        Each plugin is executed independently inside its own try/except.
        A plugin that raises is recorded as :attr:`SourceStatus.ERROR`
        and execution continues with the next one — a single broken
        plugin never stops discovery.

        Results are returned raw. No deduplication, ranking, or fixture
        fallback happens here; see the module docstring.

        Args:
            industry: Industry keyword (e.g. ``"Roofing"``).
            location: Geographic location (e.g. ``"Dallas Texas"``).
            limit: Maximum results requested per plugin. A plugin's own
                ``config.max_results`` further caps this.
            capability: Run only plugins declaring this capability.
                Accepts an enum member or a raw string. Runs all enabled
                plugins when omitted.

        Returns:
            Tuple of ``(results, diagnostics)``.
        """
        start = time.monotonic()

        registered = self._registry.get_all()
        if capability is None:
            selected = self._registry.get_enabled()
            capability_label = None
        else:
            selected = self._registry.get_by_capability(capability)
            capability_label = normalize_capability(capability)

        logger.info(
            "PluginManager.discover_all: industry=%r location=%r limit=%d "
            "capability=%s",
            industry,
            location,
            limit,
            capability_label or "any",
        )
        logger.info(
            "PluginManager: %d plugin(s) registered, %d selected: %s",
            len(registered),
            len(selected),
            [p.name for p in selected],
        )

        if not registered:
            # CLAUDE.md §5 / §12: an empty registry must never be a quiet
            # no-op. Say so at ERROR level with the reason.
            logger.error(
                "NO DISCOVERY PLUGINS REGISTERED — discovery cannot run. "
                "Register plugins via PluginManager.register() before "
                "calling discover_all()."
            )
        elif not selected:
            logger.error(
                "NO DISCOVERY PLUGINS SELECTED — %d registered but none "
                "matched (enabled=%s, capability=%s). Registered: %s",
                len(registered),
                [p.name for p in registered if p.enabled],
                capability_label or "any",
                [p.name for p in registered],
            )

        results: list[PluginRunResult] = []
        for plugin in selected:
            results.append(
                self._run_plugin(
                    plugin,
                    industry=industry,
                    location=location,
                    limit=limit,
                )
            )

        elapsed_ms = (time.monotonic() - start) * 1000
        total_companies = sum(len(r.companies) for r in results)
        total_duplicates = sum(r.duplicates_removed for r in results)

        diagnostics: dict[str, Any] = {
            "plugins_registered": len(registered),
            "plugins_selected": len(selected),
            "plugins_executed": len(results),
            "capability_filter": capability_label,
            "total_companies": total_companies,
            "duplicates_removed": total_duplicates,
            "elapsed_ms": round(elapsed_ms, 1),
            "plugin_stats": {r.plugin_name: r.to_dict() for r in results},
            "selection_reason": self._selection_reason(
                registered, selected, capability_label
            ),
        }

        logger.info(
            "PluginManager.complete: %d plugin(s) executed, %d companies "
            "collected, %d intra-plugin duplicate(s) removed (%.1fms)",
            len(results),
            total_companies,
            total_duplicates,
            elapsed_ms,
        )

        return results, diagnostics

    def _run_plugin(
        self,
        plugin: BaseDiscoveryPlugin,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> PluginRunResult:
        """Execute one plugin, isolating and classifying every failure."""
        effective_limit = min(limit, plugin.config.max_results)
        if effective_limit != limit:
            logger.debug(
                "Plugin %s: limit %d capped to %d by config.max_results",
                plugin.name,
                limit,
                effective_limit,
            )

        self._ensure_records(plugin.name)
        started = time.monotonic()

        try:
            status, companies, metadata = plugin.discover(
                industry=industry,
                location=location,
                limit=effective_limit,
            )
            latency_ms = (time.monotonic() - started) * 1000
            companies = list(companies or [])
            metadata = dict(metadata or {})
            result = PluginRunResult(
                plugin_name=plugin.name,
                status=status,
                companies=companies,
                metadata=metadata,
                latency_ms=latency_ms,
                duplicates_removed=self._read_duplicates(plugin.name, metadata),
            )
            logger.info(
                "Plugin %s [%s]: %d result(s) in %.1fms",
                plugin.name,
                getattr(status, "value", status),
                len(companies),
                latency_ms,
            )

        except Exception as exc:  # noqa: BLE001 — one plugin must not stop the run
            latency_ms = (time.monotonic() - started) * 1000
            result = PluginRunResult(
                plugin_name=plugin.name,
                status=SourceStatus.ERROR,
                metadata={"error": str(exc)},
                latency_ms=latency_ms,
                error=str(exc),
            )
            logger.error(
                "Plugin %s raised %s: %s",
                plugin.name,
                type(exc).__name__,
                exc,
                exc_info=True,
            )

        self._metrics[plugin.name].record(
            status=result.status,
            companies=len(result.companies),
            duplicates=result.duplicates_removed,
            latency_ms=result.latency_ms,
        )
        self._health[plugin.name].record_status(
            result.status,
            message=result.error,
        )
        return result

    @staticmethod
    def _read_duplicates(plugin_name: str, metadata: dict[str, Any]) -> int:
        """Read a plugin's self-reported intra-plugin duplicate count.

        A malformed value is logged and treated as zero rather than
        raising: a bad diagnostic must not fail an otherwise good run.
        """
        raw = metadata.get("duplicates_removed", 0)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            logger.warning(
                "Plugin %s reported non-numeric duplicates_removed=%r; "
                "recording 0",
                plugin_name,
                raw,
            )
            return 0

    # ------------------------------------------------------------------
    # Health checks
    # ------------------------------------------------------------------

    async def check_health(self) -> dict[str, PluginHealth]:
        """Run every registered plugin's ``health_check()``.

        Disabled plugins are marked DISABLED rather than unhealthy: not
        running is not the same as being broken.

        A plugin may return a ``state`` key naming a
        :class:`~app.discovery.plugins.plugin_health.PluginState` (for
        example ``"degraded"`` or ``"maintenance"``) to report something
        more precise than a boolean.

        Returns:
            Health records keyed by plugin name.
        """
        for plugin in self._registry.get_all():
            self._ensure_records(plugin.name)
            health = self._health[plugin.name]

            if not plugin.enabled:
                health.mark_disabled()
                continue

            try:
                report = await plugin.health_check()
                report = dict(report or {})
                health.record_check(
                    healthy=bool(report.get("healthy", False)),
                    message=str(report.get("status", "")),
                    details=report,
                    state=report.get("state"),
                )
            except Exception as exc:  # noqa: BLE001 — report, never propagate
                health.record_check(
                    healthy=False,
                    message=f"{type(exc).__name__}: {exc}",
                )
                logger.error(
                    "Plugin %s health_check raised %s: %s",
                    plugin.name,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )

        return dict(self._health)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @staticmethod
    def _selection_reason(
        registered: list[BaseDiscoveryPlugin],
        selected: list[BaseDiscoveryPlugin],
        capability_label: str | None,
    ) -> str:
        """Explain, in one line, why the selected set looks as it does."""
        if not registered:
            return "no_plugins_registered"
        if selected:
            return "ok"
        if capability_label is not None:
            return f"no_enabled_plugin_supports_{capability_label}"
        return "all_registered_plugins_disabled"

    def report(self) -> dict[str, Any]:
        """Full manager state: plugins, metrics, and health."""
        return {
            "plugins": self._registry.describe(),
            "metrics": {n: m.to_dict() for n, m in self._metrics.items()},
            "health": {n: h.to_dict() for n, h in self._health.items()},
        }

    def __repr__(self) -> str:
        return (
            f"<PluginManager plugins={len(self._registry)}"
            f" tracked={len(self._metrics)}>"
        )


# ----------------------------------------------------------------------
# Shared manager
# ----------------------------------------------------------------------

#: Process-wide shared manager, created on first use by :func:`get_manager`.
_shared_manager: PluginManager | None = None

#: Guards shared-manager creation against concurrent first access.
_shared_manager_lock = threading.Lock()


def get_manager() -> PluginManager:
    """Return the process-wide shared plugin manager.

    Metrics and health are *accumulated* state: they are only meaningful
    when they outlive a single request. The discovery pipeline builds a
    fresh :class:`~app.discovery.source_orchestrator.SourceOrchestrator`
    per search, so a manager created inline there would reset every
    plugin's counters on every call and health escalation
    (DEGRADED -> UNAVAILABLE after N consecutive failures) could never
    trigger. Sharing one manager is what makes that tracking work.

    Mirrors :func:`~app.discovery.plugins.plugin_registry.get_registry`,
    and defaults to the shared registry, so a plugin registered anywhere
    in the application is visible here.

    Construct :class:`PluginManager` directly for isolated use such as
    tests.
    """
    global _shared_manager
    if _shared_manager is None:
        with _shared_manager_lock:
            # Re-check inside the lock: another thread may have won.
            if _shared_manager is None:
                _shared_manager = PluginManager()
                logger.debug("Created shared PluginManager instance")
    return _shared_manager


def reset_manager() -> None:
    """Drop the shared manager so the next call builds a fresh one.

    Intended for tests that need to clear accumulated metrics and health
    without leaking state into the next test. Production code should not
    call this: discarding health history would erase the very signal
    that makes escalation work.
    """
    global _shared_manager
    with _shared_manager_lock:
        _shared_manager = None
        logger.debug("Reset shared PluginManager instance")
