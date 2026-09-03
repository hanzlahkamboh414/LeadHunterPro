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

    def search_many(self, queries: list[str]) -> list[list[dict[str, Any]]]:
        """Run MANY queries concurrently inside ONE event loop.

        This is the correct concurrency primitive for the aiohttp-backed
        providers: each provider holds a shared lazy ``aiohttp`` session, and
        that session must live in the SAME loop that uses it. Running one
        ``asyncio.run`` per query in parallel threads would bind the shared
        session to a first thread's loop and then fail every other thread
        with a cross-loop ``CancelledError``/``Event loop is closed``.

        So all queries run together via ``asyncio.gather`` in a single loop,
        then every provider session is closed inside that same loop — the
        exact pattern ``PlanHolderSource._default_search`` already relies on.

        Returns one list of result-dicts per query (same length/order as
        ``queries``); a failed query yields ``[]``.
        """
        if not queries:
            return []
        import asyncio

        from app.search_providers.manager import SearchProviderManager
        from app.search_providers.models import SearchQuery
        from app.search_providers.registry import get_registry

        manager = SearchProviderManager()

        async def _run_all() -> list[Any]:
            try:
                responses = await asyncio.gather(
                    *[
                        manager.search(SearchQuery(keywords=q, num_results=self._max))
                        for q in queries
                    ],
                    return_exceptions=True,
                )
            finally:
                for provider in get_registry().get_enabled():
                    close = getattr(provider, "close", None)
                    if close:
                        try:
                            await close()
                        except Exception:  # noqa: BLE001 — closing is best-effort
                            pass
            return responses

        try:
            responses = asyncio.run(_run_all())
        except Exception:  # noqa: BLE001 — a batch failure is not fatal
            return [[] for _ in queries]

        out: list[list[dict[str, Any]]] = []
        for resp in responses:
            if isinstance(resp, Exception) or not resp or not getattr(resp, "results", None):
                out.append([])
                continue
            out.append(
                [
                    {
                        "url": r.url,
                        "snippet": r.snippet or "",
                        "title": r.title or "",
                    }
                    for r in resp.results[: self._max]
                ]
            )
        return out


def _run_provider(provider: Any, query: str, num: int) -> list[dict[str, Any]]:
    """Run one (possibly async) provider, returning url/snippet/title dicts."""
    import asyncio

    async def _do():
        try:
            resp = await provider.search(SearchQuery(keywords=query, num_results=num))
            return resp
        finally:
            # Each query runs in its own event loop (asyncio.run below). A
            # provider that lazily caches an aiohttp session would otherwise
            # reuse a session bound to a now-closed loop on the next query,
            # producing "Event loop is closed" timeouts. Close it so the next
            # loop creates a fresh session.
            close = getattr(provider, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    pass

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
