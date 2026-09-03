"""Indexed-web search adapter — swappable, reuses the search-provider registry.

``IndexedWebSource.corroborate`` needs a plain ``search(query) -> list[dict]``
callable. :class:`RegistryIndexedSearch` satisfies that contract by pulling from
the shared search-provider registry (CLAUDE.md §4/§5): every enabled provider is
tried in priority order until one returns results. With no provider registered
or enabled it returns ``[]`` — the architecture never depends on a single
provider, and live search is purely additive.
"""

from __future__ import annotations

from typing import Any

from app.search_providers.models import SearchQuery
from app.search_providers.registry import get_registry


class RegistryIndexedSearch:
    """A ``search`` callable backed by the shared search-provider registry."""

    def __init__(self, max_results_per_query: int = 5) -> None:
        self._max = max_results_per_query

    def __call__(self, query: str) -> list[dict[str, Any]]:
        providers = [p for p in get_registry().get_enabled()]
        if not providers:
            return []
        for provider in providers:
            results = _run_provider(provider, query, self._max)
            if results:
                return results
        return []


def _run_provider(provider: Any, query: str, num: int) -> list[dict[str, Any]]:
    """Run one (possibly async) provider, returning url/snippet/title dicts."""
    import asyncio

    async def _do():
        resp = await provider.search(SearchQuery(keywords=query, num_results=num))
        return resp

    try:
        resp = asyncio.run(_do())
    except Exception:  # noqa: BLE001 - a provider failure is not fatal
        return []
    if not resp or not getattr(resp, "results", None):
        return []
    out: list[dict[str, Any]] = []
    for r in resp.results[:num]:
        out.append(
            {
                "url": r.url,
                "snippet": r.snippet or "",
                "title": r.title or "",
            }
        )
    return out
