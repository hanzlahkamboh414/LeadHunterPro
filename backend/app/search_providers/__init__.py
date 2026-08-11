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

    # SearXNG — register if SEARXNG_URL is configured
    searxng_url = getattr(settings, "SEARXNG_URL", "")
    if searxng_url:
        provider = SearXNGProvider(base_url=searxng_url)
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


_auto_register_providers()
del _auto_register_providers
