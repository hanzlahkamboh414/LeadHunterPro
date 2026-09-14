"""Indexed-web search adapter — swappable, reuses the search-provider registry.

``IndexedWebSource.corroborate`` needs a plain ``search(query) -> list[dict]``
callable. :class:`RegistryIndexedSearch` satisfies that contract by pulling from
the shared search-provider registry (CLAUDE.md §4/§5): every enabled provider is
tried in priority order until one returns results. With no provider registered
or enabled it returns ``[]`` — the architecture never depends on a single
provider, and live search is purely additive.

This adapter is also the ONE seam where the persistent search cache
(:mod:`app.search_providers.cache`) is applied, so every research stage —
company screening, deep research, person research, LinkedIn extract — shares a
single implementation instead of caching in four places (CLAUDE.md §14). The
cache lives above the registry and below the stages, so it stays
provider-agnostic: swapping Tavily out changes nothing here.
"""

from __future__ import annotations

import logging
from typing import Any

from app.search_providers.cache import get_search_cache
from app.search_providers.models import SearchQuery
from app.search_providers.registry import get_registry

logger = logging.getLogger(__name__)


class RegistryIndexedSearch:
    """A ``search`` callable backed by the shared search-provider registry.

    Args:
        max_results_per_query: results requested per query.
        use_cache: read/write the persistent search cache. ``False`` forces
            every query to hit a live provider (paid) — used by callers that
            must prove live behaviour rather than replay it.
    """

    def __init__(self, max_results_per_query: int = 5, *, use_cache: bool = True) -> None:
        self._max = max_results_per_query
        self._cache = get_search_cache() if use_cache else None

    def __call__(self, query: str) -> list[dict[str, Any]]:
        if self._cache is not None:
            cached = self._cache.get(query, self._max)
            if cached is not None:
                return cached
        providers = [p for p in get_registry().get_enabled()]
        if not providers:
            # CLAUDE.md §5 — never go quiet about an empty registry.
            logger.warning(
                "NO SEARCH PROVIDERS REGISTERED — live search skipped for query=%r", query
            )
            return []
        for provider in providers:
            results = _run_provider(provider, query, self._max)
            if results:
                if self._cache is not None:
                    self._cache.set(query, self._max, results)
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

        Cached queries are answered from disk and NEVER dispatched, so a
        re-researched domain or a second lead at the same company costs zero
        provider credits. Only the misses go live, in one batch.

        Returns one list of result-dicts per query (same length/order as
        ``queries``); a failed query yields ``[]``.
        """
        if not queries:
            return []
        import asyncio

        from app.search_providers.manager import SearchProviderManager
        from app.search_providers.models import SearchQuery
        from app.search_providers.registry import get_registry

        # Resolve cache hits first; only the misses are dispatched live. The
        # index map keeps the returned order identical to ``queries``.
        out: list[list[dict[str, Any]]] = [[] for _ in queries]
        pending: list[tuple[int, str]] = []
        for idx, q in enumerate(queries):
            cached = self._cache.get(q, self._max) if self._cache is not None else None
            if cached is not None:
                out[idx] = cached
            else:
                pending.append((idx, q))

        if not pending:
            logger.info(
                "SEARCH CACHE served all %d queries from cache (0 provider credits)",
                len(queries),
            )
            return out

        if self._cache is not None and len(pending) < len(queries):
            logger.info(
                "SEARCH CACHE %d/%d queries from cache, %d dispatched live",
                len(queries) - len(pending),
                len(queries),
                len(pending),
            )

        manager = SearchProviderManager()

        async def _run_all() -> list[Any]:
            try:
                responses = await asyncio.gather(
                    *[
                        manager.search(SearchQuery(keywords=q, num_results=self._max))
                        for _, q in pending
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
            return out

        # gather(return_exceptions=True) returns exactly one response per
        # pending query — strict=True is the contract, not a hope.
        for (idx, q), resp in zip(pending, responses, strict=True):
            if isinstance(resp, Exception) or not resp or not getattr(resp, "results", None):
                continue
            results = [
                {
                    "url": r.url,
                    "snippet": r.snippet or "",
                    "title": r.title or "",
                }
                for r in resp.results[: self._max]
            ]
            out[idx] = results
            if self._cache is not None:
                self._cache.set(q, self._max, results)
        return out

    def extract_many(self, urls: list[str], *, max_length: int = 4000) -> dict[str, str]:
        """Extract page text for the given URLs via the first ENABLED provider
        that advertises extraction (``supports_extract``), in ONE event loop.

        Same single-loop rule as ``search_many`` (shared aiohttp sessions must
        stay in one loop). Returns ``{url: trimmed text}`` for pages that
        yielded content; returns ``{}`` on any failure or when no enabled
        provider has extraction — extraction is additive, never fabricated,
        so the LinkedIn lane treats an empty dict as "no page readable".

        Extraction is billed PER URL, so already-extracted pages are served
        from the persistent cache and only the unseen URLs are dispatched.
        """
        if not urls:
            return {}
        import asyncio

        from app.search_providers.registry import get_registry

        out: dict[str, str] = {}
        pending: list[str] = []
        for url in urls:
            cached = (
                self._cache.get_extract(url, max_length)
                if self._cache is not None
                else None
            )
            if cached:
                out[url] = cached
            else:
                pending.append(url)
        if not pending:
            return out

        provider = next(
            (p for p in get_registry().get_enabled()
             if getattr(p, "supports_extract", False)),
            None,
        )
        if provider is None:
            return out

        async def _run() -> dict[str, str]:
            try:
                extract = getattr(provider, "extract_urls", None)
                if extract is None:
                    return {}
                return await extract(list(pending), max_length=max_length)
            finally:
                close = getattr(provider, "close", None)
                if close:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001 — closing is best-effort
                        pass

        try:
            fetched = asyncio.run(_run())
        except Exception:  # noqa: BLE001 — extract failure is not fatal
            return out
        for url, text in (fetched or {}).items():
            if not text:
                continue
            out[url] = text
            if self._cache is not None:
                self._cache.set_extract(url, max_length, text)
        return out

    def extract_one(self, url: str, *, max_length: int = 4000) -> str:
        """Extract ONE URL's text (convenience over ``extract_many``)."""
        return self.extract_many([url], max_length=max_length).get(url, "")


def _run_provider(provider: Any, query: str, num: int) -> list[dict[str, Any]]:
    """Run one (possibly async) provider, returning url/snippet/title dicts.

    Applies the SAME hard per-query cap + circuit-breaker as
    :class:`SearchProviderManager`: a hung provider (e.g. SearXNG waiting on
    dead engines) is aborted after ``timeout_s`` and marked down on the shared
    registry, so the NEXT query skips it instantly instead of paying the wait
    again.
    """
    import asyncio

    from app.search_providers.registry import get_registry

    timeout_s = getattr(provider, "timeout_s", 10.0)

    async def _do():
        try:
            resp = await asyncio.wait_for(
                asyncio.ensure_future(
                    provider.search(SearchQuery(keywords=query, num_results=num))
                ),
                timeout=timeout_s,
            )
            return resp
        except asyncio.TimeoutError:
            get_registry().mark_down(provider.provider_name)
            return None
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
