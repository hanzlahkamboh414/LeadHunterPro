"""Tests for search provider registry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.search_providers.base import BaseSearchProvider
from app.search_providers.registry import (
    SearchProviderRegistry,
    clear_registry,
    get_registry,
    register_provider,
)


class DummyProvider(BaseSearchProvider):
    """Test provider implementation."""

    def __init__(self, name: str, priority: int = 10, enabled: bool = True) -> None:
        self._name = name
        self._priority = priority
        self._enabled = enabled

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Dummy provider: {self._name}"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def priority(self) -> int:
        return self._priority

    async def search(self, query):  # type: ignore[override]
        return MagicMock()

    async def health_check(self):  # type: ignore[override]
        return {"healthy": self._enabled, "provider": self._name}


class TestSearchProviderRegistry:
    """Test SearchProviderRegistry functionality."""

    def setup_method(self):
        """Clear registry before each test."""
        clear_registry()

    def test_empty_registry(self):
        """New registry is empty."""
        reg = SearchProviderRegistry()
        assert reg.get_all() == []
        assert reg.get_enabled() == []
        assert reg.get_names() == []

    def test_register_provider(self):
        """Register adds provider to registry."""
        reg = SearchProviderRegistry()
        provider = DummyProvider("test1", priority=5)
        reg.register(provider)
        assert len(reg.get_all()) == 1
        assert reg.get_names() == ["test1"]

    def test_register_duplicate_overwrites(self):
        """Registering same name overwrites existing."""
        reg = SearchProviderRegistry()
        p1 = DummyProvider("same", priority=5)
        p2 = DummyProvider("same", priority=10)
        reg.register(p1)
        reg.register(p2)
        assert len(reg.get_all()) == 1
        assert reg.get("same").priority == 10

    def test_unregister(self):
        """Unregister removes provider."""
        reg = SearchProviderRegistry()
        provider = DummyProvider("test1")
        reg.register(provider)
        assert reg.unregister("test1") is True
        assert reg.get("test1") is None
        assert reg.unregister("missing") is False

    def test_get_by_name(self):
        """Get provider by name."""
        reg = SearchProviderRegistry()
        provider = DummyProvider("myprovider", priority=7)
        reg.register(provider)
        assert reg.get("myprovider") is provider
        assert reg.get("missing") is None

    def test_get_enabled_filters_disabled(self):
        """Only enabled providers are returned."""
        reg = SearchProviderRegistry()
        reg.register(DummyProvider("enabled1", enabled=True, priority=10))
        reg.register(DummyProvider("disabled1", enabled=False, priority=5))
        reg.register(DummyProvider("enabled2", enabled=True, priority=20))
        enabled = reg.get_enabled()
        names = [p.provider_name for p in enabled]
        assert names == ["enabled1", "enabled2"]

    def test_get_enabled_sorted_by_priority(self):
        """Enabled providers are sorted by priority ascending."""
        reg = SearchProviderRegistry()
        reg.register(DummyProvider("low", priority=30))
        reg.register(DummyProvider("high", priority=10))
        reg.register(DummyProvider("mid", priority=20))
        enabled = reg.get_enabled()
        assert [p.provider_name for p in enabled] == ["high", "mid", "low"]

    def test_clear(self):
        """Clear removes all providers."""
        reg = SearchProviderRegistry()
        reg.register(DummyProvider("a"))
        reg.register(DummyProvider("b"))
        reg.clear()
        assert reg.get_all() == []

    @pytest.mark.asyncio
    async def test_health_check_all(self):
        """Health check runs on all enabled providers."""
        reg = SearchProviderRegistry()
        reg.register(DummyProvider("good", enabled=True))
        reg.register(DummyProvider("bad", enabled=False))
        results = await reg.health_check_all()
        assert "good" in results
        assert results["good"]["healthy"] is True
        assert "bad" not in results  # Disabled provider skipped


class TestModuleLevelFunctions:
    """Test module-level registry functions."""

    def setup_method(self):
        clear_registry()

    def test_get_registry_singleton(self):
        """get_registry returns singleton instance."""
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2

    def test_register_provider_module_level(self):
        """Module-level register works."""
        provider = DummyProvider("mod_test", priority=5)
        register_provider(provider)
        registry = get_registry()
        assert registry.get("mod_test") is provider

    def test_clear_registry_module_level(self):
        """Module-level clear works."""
        register_provider(DummyProvider("x"))
        clear_registry()
        assert get_registry().get_all() == []
