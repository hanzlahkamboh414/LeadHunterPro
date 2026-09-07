"""Search provider configuration and registry.

Manages the lifecycle and discovery of search providers, providing a
centralized way to enable/disable providers and load them from config.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.search_providers.base import BaseSearchProvider

logger = logging.getLogger(__name__)


class SearchProviderRegistry:
    """Registry for search providers.

    Manages registration, retrieval, and ordering of providers.
    Providers are sorted by priority (lower number = higher priority, tried first).

    Also holds the per-process circuit-breaker state: a provider that times out
    or hard-fails is marked ``down`` for a short TTL so later queries skip it
    instantly and go straight to the next provider. State lives here (not on
    the manager) so every SearchProviderManager in the process shares it — a
    hung SearXNG costs ONE short timeout per process, not per query.

    Usage:
        registry = SearchProviderRegistry()
        registry.register(SearXNGProvider(base_url="https://searxng.example.com"))
        registry.register(BraveSearchProvider(api_key="..."))
        providers = registry.get_ordered()
    """

    # How long a provider that hung/failed stays skipped before being re-tried.
    DOWN_TTL_S: float = 300.0

    def __init__(self) -> None:
        """Initialize the registry."""
        self._providers: dict[str, BaseSearchProvider] = {}
        self._down_until: dict[str, float] = {}

    def register(self, provider: BaseSearchProvider) -> None:
        """Register a search provider.

        Args:
            provider: The provider instance to register.
        """
        name = provider.provider_name
        if name in self._providers:
            logger.warning("Search provider '%s' already registered, overwriting", name)
        self._providers[name] = provider
        # A re-registered provider starts clean (e.g. config hot-reload).
        self._down_until.pop(name, None)
        logger.info(
            "Registered search provider: %s (priority=%d, enabled=%s)",
            name,
            provider.priority,
            provider.enabled,
        )

    def mark_down(self, provider_name: str, ttl: float | None = None) -> None:
        """Record a provider as down so future queries skip it for ``ttl``s."""
        self._down_until[provider_name] = (
            time.monotonic() + (ttl if ttl is not None else self.DOWN_TTL_S)
        )
        logger.warning(
            "Search provider %r marked down for %.0fs (timeout/failure)",
            provider_name,
            self._down_until[provider_name] - time.monotonic(),
        )

    def is_down(self, provider_name: str) -> bool:
        """True while a provider is inside its down-window (skip it)."""
        expires = self._down_until.get(provider_name)
        if expires is None:
            return False
        if time.monotonic() >= expires:
            # TTL elapsed — allow a re-try and forget the stale marker.
            del self._down_until[provider_name]
            return False
        return True

    def clear(self) -> None:
        """Remove all providers from the registry."""
        self._providers.clear()
        self._down_until.clear()
        logger.info("Cleared all search providers from registry")

    def unregister(self, provider_name: str) -> bool:
        """Remove a provider from the registry.

        Args:
            provider_name: Name of the provider to remove.

        Returns:
            True if the provider was found and removed, False otherwise.
        """
        if provider_name in self._providers:
            del self._providers[provider_name]
            logger.info("Unregistered search provider: %s", provider_name)
            return True
        return False

    def get(self, provider_name: str) -> BaseSearchProvider | None:
        """Get a provider by name.

        Args:
            provider_name: The provider identifier.

        Returns:
            The provider instance, or None if not found.
        """
        return self._providers.get(provider_name)

    def get_all(self) -> list[BaseSearchProvider]:
        """Get all registered providers.

        Returns:
            List of all registered provider instances.
        """
        return list(self._providers.values())

    def get_enabled(self) -> list[BaseSearchProvider]:
        """Get all enabled providers sorted by priority.

        Returns:
            List of enabled provider instances, sorted by priority ascending.
        """
        enabled = [p for p in self._providers.values() if p.enabled]
        return sorted(enabled, key=lambda p: p.priority)

    def get_names(self) -> list[str]:
        """Get names of all registered providers.

        Returns:
            List of provider name strings.
        """
        return list(self._providers.keys())

    def get_enabled_names(self) -> list[str]:
        """Get names of all enabled providers.

        Returns:
            List of enabled provider name strings.
        """
        return [p.provider_name for p in self.get_enabled()]

    def clear(self) -> None:
        """Remove all providers from the registry."""
        self._providers.clear()
        logger.info("Cleared all search providers from registry")

    async def health_check_all(self) -> dict[str, dict[str, Any]]:
        """Run health checks on all enabled providers.

        Returns:
            Dict mapping provider name to health check result.
        """
        results: dict[str, dict[str, Any]] = {}
        for provider in self.get_enabled():
            try:
                health = await provider.health_check()
                results[provider.provider_name] = health
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Health check failed for %s: %s",
                    provider.provider_name,
                    exc,
                )
                results[provider.provider_name] = {
                    "healthy": False,
                    "error": str(exc),
                }
        return results


# Module-level singleton — auto-imported when the package is loaded
_registry: SearchProviderRegistry | None = None


def get_registry() -> SearchProviderRegistry:
    """Get the module-level singleton registry.

    Returns:
        The shared SearchProviderRegistry instance.
    """
    global _registry
    if _registry is None:
        _registry = SearchProviderRegistry()
    return _registry


def register_provider(provider: BaseSearchProvider) -> None:
    """Register a provider in the singleton registry.

    Args:
        provider: The provider to register.
    """
    get_registry().register(provider)


def clear_registry() -> None:
    """Clear the singleton registry (for testing)."""
    global _registry
    _registry = SearchProviderRegistry()
    logger.info("Search provider registry cleared")
