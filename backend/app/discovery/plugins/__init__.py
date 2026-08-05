"""Discovery plugin framework.

The foundation every future discovery source is built on. A plugin
implements :class:`BaseDiscoveryPlugin`, is held by the shared
:class:`PluginRegistry` (reachable via :func:`get_registry`), and is
executed by a :class:`PluginManager` which records
:class:`PluginMetrics` and :class:`PluginHealth` for it.

Framework only — this package contains no concrete plugins. Real
sources (government registries, directories, crawlers, search
providers) are added in later phases and must not be imported here,
because the framework may never depend on any single provider
(CLAUDE.md §4).

Status vocabulary is reused from Phase 1
(:mod:`app.discovery.sources.status`) rather than re-invented:
:class:`PluginHealth` stores the richer :class:`PluginState` and derives
a Phase 1 :class:`~app.discovery.sources.status.SourceHealth` from it,
so plugins and legacy sources stay comparable.

Typical startup wiring::

    from app.discovery.plugins import get_registry

    get_registry().register(MyPlugin())
"""

from __future__ import annotations

from app.discovery.plugins.base_plugin import (
    BaseDiscoveryPlugin,
    PluginCapability,
    normalize_capability,
)
from app.discovery.plugins.plugin_config import PluginConfig
from app.discovery.plugins.plugin_health import (
    STATE_TO_SOURCE_HEALTH,
    PluginHealth,
    PluginState,
    normalize_state,
)
from app.discovery.plugins.plugin_manager import (
    PluginManager,
    PluginRunResult,
    get_manager,
    reset_manager,
)
from app.discovery.plugins.plugin_metrics import PluginMetrics
from app.discovery.plugins.plugin_registry import (
    DuplicatePluginError,
    PluginRegistry,
    get_registry,
    plugin_registry,
)

__all__ = [
    "STATE_TO_SOURCE_HEALTH",
    "BaseDiscoveryPlugin",
    "DuplicatePluginError",
    "PluginCapability",
    "PluginConfig",
    "PluginHealth",
    "PluginManager",
    "PluginMetrics",
    "PluginRegistry",
    "PluginRunResult",
    "PluginState",
    "get_manager",
    "get_registry",
    "normalize_capability",
    "normalize_state",
    "plugin_registry",
    "reset_manager",
]
