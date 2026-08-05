"""Discovery sources package.

Each source implements BaseSource and can be plugged into
the SourceOrchestrator independently.

Sources execute in priority order. A single source failure NEVER
stops discovery — the orchestrator continues with the next source.
"""

from __future__ import annotations

from app.discovery.sources.base_source import BaseSource
from app.discovery.sources.fixture_source import FixtureSource
from app.discovery.sources.search_provider_source import SearchProviderSource

# NOTE: `plugin_source` is deliberately NOT re-exported here.
#
# `app.discovery.plugins.base_plugin` imports `app.discovery.sources.status`,
# which initializes THIS package. If this __init__ imported plugin_source,
# then importing anything from `app.discovery.plugins` first would produce:
#
#     plugins/__init__ -> base_plugin -> sources/__init__ -> plugin_source
#         -> base_plugin (partially initialized, PluginCapability undefined)
#         -> ImportError
#
# The dependency runs plugins -> sources, so sources must never depend on
# plugins. Import the adapter by module path instead:
#
#     from app.discovery.sources.plugin_source import attach_plugin_source

__all__ = [
    "BaseSource",
    "FixtureSource",
    "SearchProviderSource",
]
