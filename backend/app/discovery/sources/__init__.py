"""Discovery sources package.

Each source implements BaseSource and can be plugged into
the SourceOrchestrator independently.
"""

from __future__ import annotations

from app.discovery.sources.base_source import BaseSource
from app.discovery.sources.fixture_source import FixtureSource
from app.discovery.sources.search_provider_source import SearchProviderSource

__all__ = [
    "BaseSource",
    "FixtureSource",
    "SearchProviderSource",
]
