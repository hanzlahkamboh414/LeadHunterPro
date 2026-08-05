"""Plugin registry — the catalogue of known discovery plugins.

The registry does one job: it holds plugins and answers questions about
them (which exist, which are enabled, which run first, which can do X).
It never executes a plugin. Execution belongs to
:class:`~app.discovery.plugins.plugin_manager.PluginManager`, and the
split keeps "what is available" independently testable from "what
happened when we ran it".

Single shared registry
----------------------
The application uses one shared registry, reachable from anywhere via
:func:`get_registry` (or the :data:`plugin_registry` module alias).
Plugins are registered once at startup and every consumer sees the same
catalogue, which matches the house pattern established by
:class:`~app.connectors.connector_registry.ConnectorRegistry`.

The constructor remains public so tests can build an isolated registry
without touching global state, and :class:`PluginManager` accepts any
registry instance. The singleton is the default, not a straitjacket.

:meth:`PluginRegistry.clear` empties the registry **in place** and never
rebinds the singleton, so a module-level ``from ... import
plugin_registry`` alias can never go stale.

Duplicate registration
----------------------
A duplicate is skipped, not fatal:

    ``replace=False`` (default)
        log a warning, keep the original plugin, skip the duplicate,
        return ``False``.

    ``replace=True``
        overwrite the existing plugin, return ``True``.

This is the production-safe choice: one plugin's registration mistake at
startup cannot take down the whole application. The return value gives
callers a programmatic signal, so "skipped" is never silent — it is both
logged and returned.
"""

from __future__ import annotations

import logging
import threading

from app.discovery.plugins.base_plugin import (
    BaseDiscoveryPlugin,
    PluginCapability,
    normalize_capability,
)

logger = logging.getLogger(__name__)


class DuplicatePluginError(ValueError):
    """Raised when a duplicate registration cannot be resolved.

    Retained for backward compatibility and no longer raised by
    :meth:`PluginRegistry.register`, which now skips duplicates. Kept as
    a public name so any ``except DuplicatePluginError`` handler still
    imports and behaves correctly.
    """


class PluginRegistry:
    """Holds the set of known discovery plugins.

    Prefer the shared instance from :func:`get_registry`. Construct
    directly only for isolated use such as tests.

    Usage:
        registry = get_registry()
        registry.register(TexasSOSPlugin())
        for plugin in registry.get_enabled():
            ...
    """

    #: Process-wide shared instance, created on first use.
    _instance: PluginRegistry | None = None
    #: Guards singleton creation against concurrent first access.
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._plugins: dict[str, BaseDiscoveryPlugin] = {}

    # ------------------------------------------------------------------
    # Singleton access
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> PluginRegistry:
        """Return the process-wide shared registry, creating it if needed."""
        if cls._instance is None:
            with cls._instance_lock:
                # Re-check inside the lock: another thread may have won.
                if cls._instance is None:
                    cls._instance = cls()
                    logger.debug("Created shared PluginRegistry instance")
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Clear the shared registry in place.

        Deliberately does **not** rebind :attr:`_instance`: any module
        that captured the singleton keeps a valid reference to a now-empty
        registry, so an alias can never point at a stale object.
        """
        if cls._instance is not None:
            cls._instance.clear()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        plugin: BaseDiscoveryPlugin,
        *,
        replace: bool = False,
    ) -> bool:
        """Register a plugin.

        Args:
            plugin: The plugin instance to register.
            replace: Overwrite an already-registered plugin of the same
                name instead of skipping the duplicate.

        Returns:
            True if the plugin was registered (or replaced), False if a
            duplicate was skipped.

        Raises:
            TypeError: If ``plugin`` is not a :class:`BaseDiscoveryPlugin`.
            ValueError: If the plugin has no name.
        """
        if not isinstance(plugin, BaseDiscoveryPlugin):
            raise TypeError(
                f"Expected BaseDiscoveryPlugin, got {type(plugin).__name__}"
            )
        if not plugin.name or not plugin.name.strip():
            raise ValueError("Plugin must declare a non-empty 'name'")

        existing = self._plugins.get(plugin.name)
        if existing is not None:
            if not replace:
                # Never fatal, never silent: the original is kept, the
                # skip is logged, and False is returned so a caller can
                # react programmatically (CLAUDE.md §12).
                logger.warning(
                    "Plugin '%s' is already registered (%s); skipping "
                    "duplicate registration of %s. Pass replace=True to "
                    "override it deliberately.",
                    plugin.name,
                    type(existing).__name__,
                    type(plugin).__name__,
                )
                return False
            logger.warning(
                "Replacing registered plugin '%s': %s -> %s",
                plugin.name,
                type(existing).__name__,
                type(plugin).__name__,
            )

        self._plugins[plugin.name] = plugin
        logger.info(
            "Registered plugin: %s (priority=%d, enabled=%s, capabilities=%s)",
            plugin.name,
            plugin.priority,
            plugin.enabled,
            list(plugin.normalized_capabilities),
        )
        return True

    def unregister(self, name: str) -> bool:
        """Remove a plugin by name.

        Args:
            name: Plugin identifier.

        Returns:
            True if a plugin was removed, False if the name was unknown.
        """
        if name not in self._plugins:
            logger.debug("Cannot unregister unknown plugin: %s", name)
            return False
        del self._plugins[name]
        logger.info("Unregistered plugin: %s", name)
        return True

    def clear(self) -> None:
        """Remove every registered plugin, in place."""
        count = len(self._plugins)
        self._plugins.clear()
        logger.info("Cleared plugin registry (%d plugin(s) removed)", count)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> BaseDiscoveryPlugin | None:
        """Return a plugin by name, or None if it is not registered."""
        return self._plugins.get(name)

    def get_all(self) -> list[BaseDiscoveryPlugin]:
        """Return every plugin in priority order (lower runs first)."""
        return sorted(self._plugins.values(), key=lambda p: p.priority)

    def get_enabled(self) -> list[BaseDiscoveryPlugin]:
        """Return only enabled plugins, in priority order."""
        return [p for p in self.get_all() if p.enabled]

    def get_by_capability(
        self,
        capability: PluginCapability | str,
        *,
        enabled_only: bool = True,
    ) -> list[BaseDiscoveryPlugin]:
        """Return plugins declaring a capability, in priority order.

        Accepts an unknown capability string as readily as a declared
        enum member, so a new discovery type needs no framework change.

        Args:
            capability: The capability to filter on.
            enabled_only: Exclude disabled plugins.

        Returns:
            Matching plugins, ordered by priority.
        """
        wanted = normalize_capability(capability)
        candidates = self.get_enabled() if enabled_only else self.get_all()
        return [p for p in candidates if wanted in p.normalized_capabilities]

    def names(self) -> list[str]:
        """Return registered plugin names in priority order."""
        return [p.name for p in self.get_all()]

    def capabilities(self) -> set[str]:
        """Return the union of every registered plugin's capabilities."""
        found: set[str] = set()
        for plugin in self._plugins.values():
            found.update(plugin.normalized_capabilities)
        return found

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    def enable(self, name: str) -> bool:
        """Enable a registered plugin.

        Returns:
            True if the plugin exists, False otherwise.
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            logger.warning("Cannot enable unknown plugin: %s", name)
            return False
        plugin.enabled = True
        plugin.config.enabled = True
        logger.info("Enabled plugin: %s", name)
        return True

    def disable(self, name: str) -> bool:
        """Disable a registered plugin without unregistering it.

        Returns:
            True if the plugin exists, False otherwise.
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            logger.warning("Cannot disable unknown plugin: %s", name)
            return False
        plugin.enabled = False
        plugin.config.enabled = False
        logger.info("Disabled plugin: %s", name)
        return True

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def describe(self) -> list[dict[str, object]]:
        """Serialize every plugin's identity, in priority order."""
        return [p.describe() for p in self.get_all()]

    def __contains__(self, name: object) -> bool:
        return name in self._plugins

    def __len__(self) -> int:
        return len(self._plugins)

    def __iter__(self):
        """Iterate plugins in priority order."""
        return iter(self.get_all())

    def __repr__(self) -> str:
        return (
            f"<PluginRegistry plugins={len(self._plugins)}"
            f" enabled={len(self.get_enabled())}>"
        )


def get_registry() -> PluginRegistry:
    """Return the process-wide shared plugin registry.

    The canonical way to reach the registry from application code::

        from app.discovery.plugins import get_registry

        get_registry().register(MyPlugin())
    """
    return PluginRegistry.get_instance()


#: Module-level alias for the shared registry, for callers that prefer
#: an object to a function call. Safe to import at module scope:
#: :meth:`PluginRegistry.clear` and
#: :meth:`PluginRegistry.reset_instance` mutate in place rather than
#: rebinding, so this reference never goes stale.
plugin_registry: PluginRegistry = PluginRegistry.get_instance()
