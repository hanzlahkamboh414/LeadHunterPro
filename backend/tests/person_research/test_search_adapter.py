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


def test_extract_returns_empty_when_no_extract_provider():
    """No provider advertises extraction -> {} (additive, never fabricated)."""
    clear_registry()
    prov = _FakeProvider(
        [SearchResult(title="t", url="https://x.example.com/a", snippet="s")]
    )
    get_registry().register(prov)  # search-only provider: no supports_extract
    assert RegistryIndexedSearch().extract_many(["https://x.example.com/a"]) == {}
    assert RegistryIndexedSearch().extract_one("https://x.example.com/a") == ""
    clear_registry()


def test_extract_uses_provider_with_extract_capability():
    """extract_many routes to the first ENABLED provider that advertises
    `supports_extract`, in one event loop (same rule as search_many)."""
    clear_registry()

    class _ExtractProvider(BaseSearchProvider):
        provider_name = "fake_extract"
        enabled = True
        priority = 1
        supports_extract = True

        def __init__(self, text):
            super().__init__()
            self._text = text
            self.seen_urls = []

        async def search(self, query: SearchQuery) -> SearchResponse:
            return SearchResponse(results=[], provider=self.provider_name)

        async def extract_urls(self, urls, *, max_length=4000):
            self.seen_urls.extend(urls)
            return {u: self._text[:max_length] for u in urls}

    target = _ExtractProvider("Acme Construction — hiring estimators")
    get_registry().register(target)

    out = RegistryIndexedSearch().extract_many(["https://linkedin.com/company/acme"])
    assert out == {"https://linkedin.com/company/acme": "Acme Construction — hiring estimators"}
    assert target.seen_urls == ["https://linkedin.com/company/acme"]

    # extract_one is the single-URL convenience
    assert RegistryIndexedSearch().extract_one("https://linkedin.com/company/acme") \
        == "Acme Construction — hiring estimators"
    clear_registry()


def test_extract_prefers_priority_but_skips_non_extract():
    """A higher-priority provider WITHOUT extract does not block a lower-priority
    one that has it — capability gates the pick, then priority order."""
    clear_registry()

    class _SearchOnly(BaseSearchProvider):
        provider_name = "fake_searchonly"
        enabled = True
        priority = 0  # higher priority than the extract provider

        async def search(self, query: SearchQuery) -> SearchResponse:
            return SearchResponse(results=[], provider=self.provider_name)

    class _ExtractProvider(BaseSearchProvider):
        provider_name = "fake_extract2"
        enabled = True
        priority = 10
        supports_extract = True

        async def search(self, query: SearchQuery) -> SearchResponse:
            return SearchResponse(results=[], provider=self.provider_name)

        async def extract_urls(self, urls, *, max_length=4000):
            return {"https://x.example.com/page": "content"}

    get_registry().register(_SearchOnly())
    get_registry().register(_ExtractProvider())

    assert RegistryIndexedSearch().extract_many(["https://x.example.com/page"]) \
        == {"https://x.example.com/page": "content"}
    clear_registry()


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
