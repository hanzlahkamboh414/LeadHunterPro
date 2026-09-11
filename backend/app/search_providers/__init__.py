"""Search provider infrastructure for LeadHunter Pro.

Provides a pluggable search provider system that enables the discovery
pipeline to query public search sources (SearXNG, Brave Search API, etc.)
for construction company websites.

Architecture:
    BaseSearchProvider  — abstract interface all providers implement
    SearchProviderRegistry — singleton managing provider lifecycle
    SearXNGProvider     — self-hosted metasearch engine
    BraveSearchProvider — Brave Search REST API
    SearchQuery/Response — standardized data models

Usage:
    from app.search_providers.registry import get_registry
    from app.search_providers.searxng import SearXNGProvider
    from app.search_providers.models import SearchQuery

    registry = get_registry()
    registry.register(SearXNGProvider(base_url="https://searxng.example.com"))

    query = SearchQuery(keywords="roofing dallas tx", num_results=10)
    response = await registry.dispatch(query)
"""

from __future__ import annotations

import logging
logger = logging.getLogger(__name__)

# Public exports
from app.search_providers.base import BaseSearchProvider
from app.search_providers.brave import (
    BraveAPIError,
    BraveAuthError,
    BraveSearchProvider,
)
from app.search_providers.models import SearchQuery, SearchResponse, SearchResult
from app.search_providers.registry import (
    SearchProviderRegistry,
    clear_registry,
    get_registry,
    register_provider,
)
from app.search_providers.searxng import (
    SearXNGConfigError,
    SearXNGHTTPError,
    SearXNGProvider,
)
from app.search_providers.tavily import (
    TavilyAPIError,
    TavilyAuthError,
    TavilySearchProvider,
)

__all__ = [
    "BaseSearchProvider",
    "BraveAPIError",
    "BraveAuthError",
    "BraveSearchProvider",
    "SearXNGConfigError",
    "SearXNGHTTPError",
    "SearXNGProvider",
    "TavilyAPIError",
    "TavilyAuthError",
    "TavilySearchProvider",
    "SearchProviderRegistry",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
    "clear_registry",
    "get_registry",
    "register_provider",
]


# ---------------------------------------------------------------------------
# Auto-registration on import — reads config and registers available providers
# ---------------------------------------------------------------------------
def _auto_register_providers() -> None:
    """Register search providers based on configured environment.

    Reads SEARXNG_URL and BRAVE_SEARCH_API_KEY from application settings.
    Only registers providers when their required configuration is present.
    """
    try:
        from app.core.config import settings
    except ImportError:
        return  # Settings not available yet (e.g., in tests)

    registry = get_registry()

    # SearXNG — register if SEARXNG_URL is configured. The timeout comes from
    # SEARXNG_TIMEOUT (default 6s): SearXNG aggregates many engines (DuckDuckGo
    # included, which is routinely slow/blocked), so without a short cap a hung
    # instance stalls every query and the run creeps.
    searxng_url = getattr(settings, "SEARXNG_URL", "")
    if searxng_url:
        searchng_timeout = getattr(settings, "SEARXNG_TIMEOUT", None)
        provider = SearXNGProvider(
            base_url=searxng_url,
            timeout=searchng_timeout if searchng_timeout is not None else 6,
        )
        registry.register(provider)
        logger.info("Auto-registered SearXNG provider: %s", searxng_url)

    # Brave Search — register if API key is configured
    brave_key = getattr(settings, "BRAVE_SEARCH_API_KEY", "")
    if brave_key:
        provider = BraveSearchProvider(api_key=brave_key)
        registry.register(provider)
        logger.info("Auto-registered Brave Search provider")

    # Tavily Search — register if API key is configured
    tavily_key = getattr(settings, "TAVILY_SEARCH_API_KEY", "")
    if tavily_key:
        provider = TavilySearchProvider(api_key=tavily_key)
        registry.register(provider)
        logger.info("Auto-registered Tavily Search provider")


def re_register_configured_providers() -> dict[str, str]:
    """Rebuild search provider instances from CURRENT settings (hot-reload).

    Called by the admin key endpoint right after a TAVILY/BRAVE key changes:
    a FRESH instance carrying the new key replaces the old one in the registry
    (``register`` also clears a stale down-marker, so a provider that was
    blacklisted on the dead key gets a clean retry). A key that is now empty
    UNREGISTERS its provider — the next query must not keep trying a dead key.

    This is a real switch, not a fake one (CLAUDE.md §6): the old instance is
    genuinely replaced, and only searches already in flight on it complete
    with the old key. SearXNG is untouched here — it is keyed on SEARXNG_URL
    (config, not a managed secret).

    Returns:
        Human-readable action strings (e.g. ``"rebuilt tavily"``,
        ``"removed brave"``) for the honest admin log line — an empty list
        means nothing changed.
    """
    from app.core.config import settings

    registry = get_registry()
    actions: list[str] = []

    for name, key, cls in (
        ("tavily", getattr(settings, "TAVILY_SEARCH_API_KEY", "") or "", TavilySearchProvider),
        ("brave", getattr(settings, "BRAVE_SEARCH_API_KEY", "") or "", BraveSearchProvider),
    ):
        if key:
            registry.register(cls(api_key=key))
            actions.append(f"rebuilt {name}")
        elif registry.get(name) is not None:
            registry.unregister(name)
            actions.append(f"removed {name}")

    return actions


_auto_register_providers()
del _auto_register_providers
