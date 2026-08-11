"""Offline tests for BaseIntentPlugin (Increment 5).

Pins the intent-plugin contract: an intent plugin registers and filters
by capability like any discovery plugin, but its ``discover()`` returns
an honest ``EMPTY`` (it is not a company-discovery source), and the real
entry point is ``collect_evidence``.
"""

from __future__ import annotations

import pytest

from app.discovery.intent.base import BaseIntentPlugin
from app.discovery.plugins.base_plugin import PluginCapability
from app.discovery.plugins.plugin_registry import PluginRegistry
from app.discovery.sources.status import SourceStatus


class _TestIntentPlugin(BaseIntentPlugin):
    """Minimal concrete intent plugin for contract tests."""

    name = "test_intent"
    capabilities = (PluginCapability.PROJECT_DISCOVERY,)

    def collect_evidence(self, *, company_name, website="", location=""):
        return SourceStatus.EMPTY, [], {"source": self.name}


class TestBaseIntentPlugin:

    def test_base_is_abstract(self):
        """The base cannot be instantiated — collect_evidence must exist."""
        with pytest.raises(TypeError):
            BaseIntentPlugin()  # type: ignore[abstract]

    def test_discover_is_honest_empty(self):
        """An intent plugin never fabricates company-discovery results."""
        plugin = _TestIntentPlugin()
        status, companies, metadata = plugin.discover(
            industry="Roofing",
            location="Dallas Texas",
            limit=10,
        )
        assert status is SourceStatus.EMPTY
        assert companies == []
        assert "intent plugin" in metadata["note"]

    def test_registers_and_filters_by_capability(self):
        registry = PluginRegistry()
        registry.register(_TestIntentPlugin())

        assert registry.get_by_capability(PluginCapability.PROJECT_DISCOVERY)
        # Intent plugins never leak into the company-discovery stage.
        assert registry.get_by_capability(PluginCapability.COMPANY_DISCOVERY) == []
