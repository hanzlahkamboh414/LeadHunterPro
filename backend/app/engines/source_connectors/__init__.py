"""Construction Source Connectors – framework and implementations.

Provides a composable connector interface for discovering construction
companies from industry-specific public sources (procurement portals,
trade directories, bid databases).

No Google/Bing/DuckDuckGo – only purpose-built construction sources.

All core SDK types live in ``sdk.py``; this package re-exports them
for convenient top-level access.
"""

from __future__ import annotations

# Core SDK types (canonical definitions live in sdk.py)
from app.engines.source_connectors.sdk import (  # noqa: F401
    BaseConnector,
    CompanyResult,
    ConnectorConfig,
    ConstructionSourceRegistry,
    ConnectorRegistry,
    get_connector,
    list_connectors,
)

# SDK utilities
from app.engines.source_connectors.connector_logger import ConnectorLogger  # noqa: F401
from app.engines.source_connectors.deduplicator import Deduplicator  # noqa: F401
from app.engines.source_connectors.error_handler import ErrorHandler  # noqa: F401
from app.engines.source_connectors.http_client import HTTPClient  # noqa: F401
from app.engines.source_connectors.normalizer import Normalizer  # noqa: F401
from app.engines.source_connectors.rate_limiter import RateLimiter  # noqa: F401
from app.engines.source_connectors.retry_manager import (  # noqa: F401
    RetryManager,
    connector_retry,
)

# Other components
from app.engines.source_connectors.evidence import Evidence  # noqa: F401
from app.engines.source_connectors.connector_manager import ConnectorManager  # noqa: F401
from app.engines.source_connectors.mock_connector import MockConnector  # noqa: F401

# Built-in connectors (trigger registration at import time)
from app.engines.source_connectors.texas_procurement import TexasProcurementConnector  # noqa: F401
from app.engines.source_connectors.agc_texas import AgcTexasConnector  # noqa: F401

__all__ = [
    "BaseConnector",
    "CompanyResult",
    "ConnectorConfig",
    "ConnectorRegistry",
    "ConstructionSourceRegistry",
    "get_connector",
    "list_connectors",
    "ConnectorLogger",
    "Deduplicator",
    "ErrorHandler",
    "HTTPClient",
    "Normalizer",
    "RateLimiter",
    "RetryManager",
    "connector_retry",
    "Evidence",
    "ConnectorManager",
    "MockConnector",
    "TexasProcurementConnector",
    "AgcTexasConnector",
]
