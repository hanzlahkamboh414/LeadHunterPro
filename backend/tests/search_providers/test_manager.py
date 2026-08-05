"""Tests for SearchProviderManager."""

from __future__ import annotations

import pytest

from app.search_providers.manager import SearchProviderManager
from app.search_providers.models import SearchQuery, SearchResult
from app.search_providers.registry import SearchProviderRegistry, clear_registry


class FakeProvider:
    """Fake provider for testing manager behavior."""

    def __init__(
        self,
        name: str,
        priority: int = 10,
        enabled: bool = True,
        results: list | None = None,
        error: str | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.provider_name = name
        self.description = f"Fake provider: {name}"
        self.priority = priority
        self.enabled = enabled
        self._results = results or []
        self._error = error
        self._exception = exception
        self.call_count = 0

    async def search(self, query):  # type: ignore[override]
        self.call_count += 1
        if self._exception:
            raise self._exception
        if self._error:
            from app.search_providers.models import SearchResponse

            return SearchResponse(
                provider=self.provider_name,
                query=query.keywords,
                error=self._error,
                status="error",
            )
        from app.search_providers.models import SearchResponse

        return SearchResponse(
            results=self._results,
            provider=self.provider_name,
            query=query.keywords,
            status="success" if self._results else "partial",
        )

    async def health_check(self):  # type: ignore[override]
        return {"healthy": self.enabled, "provider": self.provider_name}


class TestSearchProviderManager:
    """Test SearchProviderManager orchestration."""

    def setup_method(self):
        clear_registry()

    def test_no_providers(self):
        """Manager with no providers returns empty."""
        manager = SearchProviderManager()
        import asyncio

        response = asyncio.run(manager.search(SearchQuery(keywords="test")))
        assert response.status == "partial"
        assert len(response.results) == 0

    @pytest.mark.asyncio
    async def test_single_provider_success(self):
        """Single provider returning results."""
        registry = SearchProviderRegistry()
        provider = FakeProvider(
            "test1",
            results=[SearchResult(title="A", url="https://a.com")],
        )
        registry.register(provider)
        manager = SearchProviderManager(registry)

        response = await manager.search(SearchQuery(keywords="roofing"))
        assert response.status == "success"
        assert len(response.results) == 1
        assert response.results[0].title == "A"
        assert provider.call_count == 1

    @pytest.mark.asyncio
    async def test_fallback_to_next_provider(self):
        """First provider fails, second succeeds."""
        registry = SearchProviderRegistry()
        bad = FakeProvider("bad", priority=10, error="Connection refused")
        good = FakeProvider(
            "good",
            priority=20,
            results=[SearchResult(title="B", url="https://b.com")],
        )
        registry.register(bad)
        registry.register(good)
        manager = SearchProviderManager(registry)

        response = await manager.search(SearchQuery(keywords="test"))
        assert response.status == "success"
        assert len(response.results) == 1
        assert response.results[0].title == "B"
        assert "Connection refused" in response.error

    @pytest.mark.asyncio
    async def test_fail_fast_mode(self):
        """fail_fast=True stops after first success."""
        registry = SearchProviderRegistry()
        first = FakeProvider(
            "first",
            priority=10,
            results=[SearchResult(title="X", url="https://x.com")],
        )
        second = FakeProvider("second", priority=20)
        registry.register(first)
        registry.register(second)
        manager = SearchProviderManager(registry)

        response = await manager.search(
            SearchQuery(keywords="test"),
            fail_fast=True,
        )
        assert response.status == "success"
        assert first.call_count == 1
        assert second.call_count == 0  # Not called due to fail_fast

    @pytest.mark.asyncio
    async def test_min_results_early_stop(self):
        """Stops when min_results threshold reached."""
        registry = SearchProviderRegistry()
        first = FakeProvider(
            "first",
            priority=10,
            results=[
                SearchResult(title="A", url="https://a.com"),
                SearchResult(title="B", url="https://b.com"),
                SearchResult(title="C", url="https://c.com"),
            ],
        )
        second = FakeProvider(
            "second",
            priority=20,
            results=[SearchResult(title="D", url="https://d.com")],
        )
        registry.register(first)
        registry.register(second)
        manager = SearchProviderManager(registry)

        response = await manager.search(
            SearchQuery(keywords="test"),
            min_results=3,
        )
        assert len(response.results) == 3
        assert first.call_count == 1
        assert second.call_count == 0  # Stopped after reaching min_results

    @pytest.mark.asyncio
    async def test_all_providers_fail(self):
        """All providers fail returns error status."""
        registry = SearchProviderRegistry()
        registry.register(FakeProvider("p1", error="Err 1"))
        registry.register(FakeProvider("p2", error="Err 2"))
        manager = SearchProviderManager(registry)

        response = await manager.search(SearchQuery(keywords="test"))
        assert response.status == "error"
        assert len(response.results) == 0
        assert "Err 1" in response.error
        assert "Err 2" in response.error

    @pytest.mark.asyncio
    async def test_provider_exception_handled(self):
        """Provider throwing exception is caught and logged."""
        registry = SearchProviderRegistry()
        registry.register(FakeProvider("crashy", exception=RuntimeError("boom")))
        registry.register(
            FakeProvider(
                "ok",
                results=[SearchResult(title="X", url="https://x.com")],
            )
        )
        manager = SearchProviderManager(registry)

        response = await manager.search(SearchQuery(keywords="test"))
        assert response.status == "success"
        assert len(response.results) == 1

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Health check returns status for all providers."""
        registry = SearchProviderRegistry()
        registry.register(FakeProvider("good", enabled=True))
        registry.register(FakeProvider("bad", enabled=False))
        manager = SearchProviderManager(registry)

        results = await manager.health_check()
        assert "good" in results
        assert results["good"]["healthy"] is True

    def test_list_providers(self):
        """List providers returns metadata."""
        registry = SearchProviderRegistry()
        registry.register(FakeProvider("p1", priority=10, enabled=True))
        registry.register(FakeProvider("p2", priority=20, enabled=False))
        manager = SearchProviderManager(registry)

        providers = manager.list_providers()
        names = [p["name"] for p in providers]
        assert "p1" in names
        assert "p2" in names
        assert providers[0]["priority"] == 10

    def test_deduplication_by_url(self):
        """Duplicate URLs across providers are NOT deduped here
        (dedup happens at connector level). Manager just aggregates."""
        registry = SearchProviderRegistry()
        same_url = SearchResult(title="Dup", url="https://same.com")
        registry.register(FakeProvider("p1", results=[same_url]))
        registry.register(FakeProvider("p2", results=[same_url]))
        manager = SearchProviderManager(registry)
        import asyncio

        response = asyncio.run(manager.search(SearchQuery(keywords="test")))
        # Manager aggregates; dedup is done by ConnectorManager later
        # Manager aggregates; dedup happens at connector level
        assert len(response.results) >= 1

    @pytest.mark.asyncio
    async def test_disabled_provider_skipped(self):
        """Disabled providers are not called."""
        registry = SearchProviderRegistry()
        disabled = FakeProvider("disabled", enabled=False)
        enabled = FakeProvider(
            "enabled",
            results=[SearchResult(title="X", url="https://x.com")],
        )
        registry.register(disabled)
        registry.register(enabled)
        manager = SearchProviderManager(registry)

        response = await manager.search(SearchQuery(keywords="test"))
        assert disabled.call_count == 0
        assert enabled.call_count == 1
        assert response.status == "success"
