"""Offline tests for intent-plugin registration (Increment 5).

Pins the startup seam: ``register_intent_plugins`` registers all three
plugins into a registry, is idempotent (duplicates skipped, never fatal),
and keeps each plugin's capability isolated so none can leak into the
company-discovery pipeline.
"""

from __future__ import annotations

from app.discovery.intent import (
    GoogleNewsPlugin,
    USAspendingPlugin,
    register_intent_plugins,
)
from app.discovery.plugins.base_plugin import PluginCapability
from app.discovery.plugins.plugin_registry import PluginRegistry


class TestRegisterIntentPlugins:

    def test_registers_all_three(self):
        registry = PluginRegistry()
        names = register_intent_plugins(registry)
        assert sorted(names) == ["company_site", "google_news", "usaspending"]
        assert len(registry) == 3

    def test_idempotent(self):
        registry = PluginRegistry()
        register_intent_plugins(registry)
        second = register_intent_plugins(registry)
        assert second == []
        assert len(registry) == 3

    def test_capability_isolation(self):
        registry = PluginRegistry()
        register_intent_plugins(registry)
        by_capability = {
            PluginCapability.PROJECT_DISCOVERY: "company_site",
            PluginCapability.NEWS_DISCOVERY: "google_news",
            PluginCapability.BID_DISCOVERY: "usaspending",
        }
        for capability, expected in by_capability.items():
            names = [p.name for p in registry.get_by_capability(capability)]
            assert names == [expected], capability
        # none of the intent plugins ever supports company discovery
        assert registry.get_by_capability(PluginCapability.COMPANY_DISCOVERY) == []

    def test_plugins_are_constructed_offline(self):
        """Building a plugin (even company_site's default fetcher) does no I/O."""
        assert GoogleNewsPlugin().name == "google_news"
        assert USAspendingPlugin().name == "usaspending"
