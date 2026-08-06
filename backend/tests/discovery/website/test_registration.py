"""Tests for the website discovery composition root (registration).

Phase 1 wiring: the plugin is built with its concrete generators via
dependency injection and handed to the framework's registration
mechanism (``PluginRegistry``). These tests pin three things:

- the DI set — the plugin is built with Brave Search, SearXNG and the
  fixture bridge;
- the config gate — with no live origin configured nothing is
  registered, so a fixture-only website discovery path can never become
  the pipeline's first and only source (CLAUDE.md §1);
- idempotency — a composition root that runs per discovery call never
  rebuilds or warns.

The shared registry is reset around every test so no plugin leaks
between tests and no test depends on collection order.
"""

from __future__ import annotations

import pytest

from app.discovery.plugins.plugin_registry import PluginRegistry, get_registry
from app.discovery.website.generators import (
    BraveSearchGenerator,
    FixtureGenerator,
    SearXNGGenerator,
)
from app.discovery.website.plugin import DirectWebsiteDiscoveryPlugin
from app.discovery.website.registration import (
    build_website_discovery_plugin,
    register_website_discovery,
)


@pytest.fixture(autouse=True)
def _reset_shared_registry():
    PluginRegistry.reset_instance()
    yield
    PluginRegistry.reset_instance()


class TestBuild:
    """build_website_discovery_plugin wires the three concrete generators."""

    def test_builds_plugin_with_the_three_generators(self):
        plugin = build_website_discovery_plugin()
        assert isinstance(plugin, DirectWebsiteDiscoveryPlugin)
        assert isinstance(plugin._generators[0], BraveSearchGenerator)
        assert isinstance(plugin._generators[1], SearXNGGenerator)
        assert isinstance(plugin._generators[2], FixtureGenerator)

    def test_build_accepts_injected_generators(self):
        gen = FixtureGenerator()
        plugin = build_website_discovery_plugin(generators=[gen])
        assert plugin._generators == [gen]

    def test_built_plugin_is_a_discovery_plugin(self):
        plugin = build_website_discovery_plugin()
        assert plugin.supports("company_discovery")


class TestRegister:
    """register_website_discovery gates on live origin and is idempotent."""

    def test_gated_off_without_live_origin(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", "")
        monkeypatch.setattr(settings, "SEARXNG_URL", "")

        assert register_website_discovery() is False
        assert "direct_website_discovery" not in get_registry()

    def test_gated_off_never_constructs_generators(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", "")
        monkeypatch.setattr(settings, "SEARXNG_URL", "")

        register_website_discovery()
        assert len(get_registry()) == 0

    def test_registers_when_brave_is_configured(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", "test-key")

        assert register_website_discovery() is True
        registry = get_registry()
        assert "direct_website_discovery" in registry
        assert isinstance(
            registry.get("direct_website_discovery"), DirectWebsiteDiscoveryPlugin
        )

    def test_registers_when_searxng_is_configured(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "SEARXNG_URL", "http://searxng.test")

        assert register_website_discovery() is True
        assert "direct_website_discovery" in get_registry()

    def test_is_idempotent(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "SEARXNG_URL", "http://searxng.test")

        assert register_website_discovery() is True
        assert register_website_discovery() is True
        assert len(get_registry()) == 1
