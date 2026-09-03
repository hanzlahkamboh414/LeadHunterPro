"""RegistryIndexedSearch — swappable indexed-web search via the registry."""

from __future__ import annotations

from app.search_providers.base import BaseSearchProvider
from app.search_providers.models import SearchQuery, SearchResponse, SearchResult
from app.search_providers.registry import clear_registry, get_registry
from app.person_research.search_adapter import RegistryIndexedSearch


class _FakeProvider(BaseSearchProvider):
    provider_name = "fake"
    enabled = True
    priority = 1

    def __init__(self, results):
        super().__init__()
        self._results = results
        self.seen_queries = []

    async def search(self, query: SearchQuery) -> SearchResponse:
        self.seen_queries.append(query.keywords)
        return SearchResponse(results=self._results, provider=self.provider_name)


def test_returns_empty_when_no_provider_registered():
    clear_registry()
    assert RegistryIndexedSearch()("anything") == []


def test_uses_registered_provider():
    clear_registry()
    prov = _FakeProvider(
        [SearchResult(title="t", url="https://x.example.com/a", snippet="John Smith jsmith@acme.com")]
    )
    get_registry().register(prov)
    out = RegistryIndexedSearch()('"jsmith@acme.com"')
    assert out == [{"url": "https://x.example.com/a", "snippet": "John Smith jsmith@acme.com", "title": "t"}]
    assert prov.seen_queries == ['"jsmith@acme.com"']
    clear_registry()


def test_tries_next_provider_on_empty():
    clear_registry()
    empty = _FakeProvider([])
    full = _FakeProvider([SearchResult(title="t", url="https://y.example.com/b", snippet="s")])
    get_registry().register(empty)
    get_registry().register(full)
    out = RegistryIndexedSearch()("q")
    assert out and out[0]["url"] == "https://y.example.com/b"
    clear_registry()
