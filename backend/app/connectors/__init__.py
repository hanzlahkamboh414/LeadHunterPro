"""Connectors package — unified interface for external data sources.

This package provides a pluggable connector architecture for discovering
companies from various sources (procurement portals, trade directories,
bid databases). Connectors are registered globally and executed via
the ConnectorManager.

Built-in connectors are automatically registered on import.
"""

from __future__ import annotations

from app.connectors.base_connector import BaseConnector
from app.connectors.connector_manager import ConnectorManager
from app.connectors.connector_registry import ConnectorRegistry
from app.connectors.connector_result import ConnectorResult
from app.connectors.industry_expansion import (
    expand_industry,
    matches_industry,
)

# Import built-in connectors (auto-registers via module-level __init__)
from app.connectors.texas_procurement import TexasProcurementConnector  # noqa: F401

__all__ = [
    "BaseConnector",
    "ConnectorManager",
    "ConnectorRegistry",
    "ConnectorResult",
    "expand_industry",
    "matches_industry",
]
