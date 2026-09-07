"""RegistryIndexedSearch + persistent cache — proof that credits are saved.

The claim under test is not "results come back" but "the provider was NOT
called". Every assertion here counts provider invocations, because that count
IS the bill.
"""

from __future__ import annotations

from app.person_research.search_adapter import RegistryIndexedSearch
from app.search_providers.base import BaseSearchProvider
from app.search_providers.cache import SearchCache
from app.search_providers.models import SearchQuery, SearchResponse, SearchResult
from app.search_providers.registry import clear_registry, get_registry


class _CountingProvider(BaseSearchProvider):
    provider_name = "counting"
    enabled = True
    priority = 1

    def __init__(self, results=None, *, fail: bool = False):
        super().__init__()
        self._results = results or [
            SearchResult(title="t", url="https://acme.example/about", snippet="s")
        ]
        self._fail = fail
        self.calls: list[str] = []

    async def search(self, query: SearchQuery) -> SearchResponse:
        self.calls.append(query.keywords)
        if self._fail:
            # Providers never raise — a quota rejection arrives as an error
            # response, which flattens to [] at the adapter.
            return SearchResponse(
                results=[], provider=self.provider_name, status="error",
                error="HTTP 432 plan usage limit exceeded",
            )
        return SearchResponse(results=self._results, provider=self.provider_name)


class _CountingExtractProvider(BaseSearchProvider):
    provider_name = "counting_extract"
    enabled = True
    priority = 1
    supports_extract = True

    def __init__(self):
        super().__init__()
        self.extracted: list[str] = []

    async def search(self, query: SearchQuery) -> SearchResponse:
        return SearchResponse(results=[], provider=self.provider_name)

    async def extract_urls(self, urls, *, max_length=4000):
        self.extracted.extend(urls)
        return {u: f"text of {u}"[:max_length] for u in urls}


def _adapter(tmp_path, max_results=5) -> RegistryIndexedSearch:
    """An adapter wired to a private cache file (never the real one)."""
    a = RegistryIndexedSearch(max_results)
    a._cache = SearchCache(tmp_path / "adapter.db")
    return a


# -- single query -----------------------------------------------------------


def test_second_identical_call_does_not_touch_the_provider(tmp_path):
    clear_registry()
    prov = _CountingProvider()
    get_registry().register(prov)

    a = _adapter(tmp_path)
    first = a('"acme.com" company')
    second = a('"acme.com" company')

    assert first == second
    assert len(prov.calls) == 1, "the second query must be served from cache"
    clear_registry()


def test_use_cache_false_always_goes_live(tmp_path):
    clear_registry()
    prov = _CountingProvider()
    get_registry().register(prov)

    a = RegistryIndexedSearch(5, use_cache=False)
    a('"acme.com"')
    a('"acme.com"')
    assert len(prov.calls) == 2
    clear_registry()


def test_provider_error_is_not_cached_and_is_retried(tmp_path):
    """A quota rejection must never be frozen in as "no results"."""
    clear_registry()
    failing = _CountingProvider(fail=True)
    get_registry().register(failing)

    a = _adapter(tmp_path)
    assert a("q") == []
    assert a("q") == []
    assert len(failing.calls) == 2, "an empty/error answer must be retried"
    clear_registry()


# -- batch (search_many) ----------------------------------------------------


def test_search_many_dispatches_only_the_misses(tmp_path):
    clear_registry()
    prov = _CountingProvider()
    get_registry().register(prov)

    a = _adapter(tmp_path)
    a.search_many(["q1", "q2"])
    assert sorted(prov.calls) == ["q1", "q2"]

    prov.calls.clear()
    out = a.search_many(["q1", "q2", "q3"])
    assert prov.calls == ["q3"], "only the unseen query may be dispatched"
    assert len(out) == 3
    assert out[0] and out[1] and out[2]
    clear_registry()


def test_search_many_preserves_order_when_mixing_hits_and_misses(tmp_path):
    clear_registry()

    class _PerQuery(BaseSearchProvider):
        provider_name = "perquery"
        enabled = True
        priority = 1

        async def search(self, query: SearchQuery) -> SearchResponse:
            return SearchResponse(
                results=[
                    SearchResult(
                        title=query.keywords,
                        url=f"https://x.example/{query.keywords}",
                        snippet=query.keywords,
                    )
                ],
                provider=self.provider_name,
            )

    get_registry().register(_PerQuery())
    a = _adapter(tmp_path)

    a.search_many(["b"])  # prime the cache for 'b' only
    out = a.search_many(["a", "b", "c"])
    assert [r[0]["title"] for r in out] == ["a", "b", "c"]
    clear_registry()


def test_search_many_all_cached_makes_no_provider_call(tmp_path):
    clear_registry()
    prov = _CountingProvider()
    get_registry().register(prov)

    a = _adapter(tmp_path)
    a.search_many(["q1", "q2", "q3"])
    prov.calls.clear()
    out = a.search_many(["q1", "q2", "q3"])
    assert prov.calls == []
    assert all(out)
    clear_registry()


def test_single_call_and_search_many_share_one_cache(tmp_path):
    """The screening stage uses search_many, other lanes use __call__ — a query
    paid for by one must be free for the other."""
    clear_registry()
    prov = _CountingProvider()
    get_registry().register(prov)

    a = _adapter(tmp_path)
    a.search_many(['"acme.com"'])
    prov.calls.clear()
    assert a('"acme.com"')
    assert prov.calls == []
    clear_registry()


def test_search_many_empty_input_is_noop(tmp_path):
    clear_registry()
    assert _adapter(tmp_path).search_many([]) == []
    clear_registry()


# -- extract ----------------------------------------------------------------


def test_extract_many_only_fetches_unseen_urls(tmp_path):
    clear_registry()
    prov = _CountingExtractProvider()
    get_registry().register(prov)

    a = _adapter(tmp_path)
    a.extract_many(["https://x.example/a"])
    assert prov.extracted == ["https://x.example/a"]

    prov.extracted.clear()
    out = a.extract_many(["https://x.example/a", "https://x.example/b"])
    assert prov.extracted == ["https://x.example/b"]
    assert out["https://x.example/a"] == "text of https://x.example/a"
    assert out["https://x.example/b"] == "text of https://x.example/b"
    clear_registry()


def test_extract_all_cached_makes_no_provider_call(tmp_path):
    clear_registry()
    prov = _CountingExtractProvider()
    get_registry().register(prov)

    a = _adapter(tmp_path)
    a.extract_many(["https://x.example/a"])
    prov.extracted.clear()
    assert a.extract_one("https://x.example/a") == "text of https://x.example/a"
    assert prov.extracted == []
    clear_registry()


def test_no_provider_registered_still_returns_cached_answers(tmp_path):
    """A cached answer stays usable even with an empty registry — the cache is
    real prior evidence, not a fixture (CLAUDE.md §1: no synthetic data)."""
    clear_registry()
    prov = _CountingProvider()
    get_registry().register(prov)
    a = _adapter(tmp_path)
    a('"acme.com"')

    clear_registry()  # every provider gone (quota dead / key removed)
    assert a('"acme.com"')  # served from the earlier real answer
    assert a("never asked before") == []  # nothing invented
    clear_registry()
