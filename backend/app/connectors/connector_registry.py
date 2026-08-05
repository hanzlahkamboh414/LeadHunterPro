"""Connector registry — manages registered connector instances.

Provides a centralized registry for all connectors with support for
registering, removing, enabling, disabling, and querying connectors.
"""

from __future__ import annotations

import logging

from app.connectors.base_connector import BaseConnector

logger = logging.getLogger(__name__)


class ConnectorRegistry:
    """Singleton registry for all source connectors.

    Connectors are registered at module import time or dynamically
    via :meth:`register`. Use :meth:`get_enabled` to retrieve active
    connectors sorted by priority.
    """

    _connectors: dict[str, BaseConnector] = {}

    @classmethod
    def register(cls, connector: BaseConnector) -> None:
        """Register a connector instance.

        Args:
            connector: Connector instance to register.
        """
        if connector.connector_name in cls._connectors:
            logger.warning(
                "Connector '%s' already registered, overwriting",
                connector.connector_name,
            )
        cls._connectors[connector.connector_name] = connector
        logger.info(
            "Registered connector: %s (priority=%d)",
            connector.connector_name,
            connector.priority,
        )

    @classmethod
    def remove(cls, connector_name: str) -> bool:
        """Remove a connector from the registry.

        Args:
            connector_name: Name of the connector to remove.

        Returns:
            True if the connector was removed, False if not found.
        """
        if connector_name in cls._connectors:
            del cls._connectors[connector_name]
            logger.info("Removed connector: %s", connector_name)
            return True
        return False

    @classmethod
    def enable(cls, connector_name: str) -> bool:
        """Enable a connector by name.

        Args:
            connector_name: Name of the connector to enable.

        Returns:
            True if the connector was found and enabled.
        """
        connector = cls._connectors.get(connector_name)
        if connector:
            connector.enabled = True
            logger.info("Enabled connector: %s", connector_name)
            return True
        return False

    @classmethod
    def disable(cls, connector_name: str) -> bool:
        """Disable a connector by name.

        Args:
            connector_name: Name of the connector to disable.

        Returns:
            True if the connector was found and disabled.
        """
        connector = cls._connectors.get(connector_name)
        if connector:
            connector.enabled = False
            logger.info("Disabled connector: %s", connector_name)
            return True
        return False

    @classmethod
    def get(cls, connector_name: str) -> BaseConnector | None:
        """Get a connector by name.

        Args:
            connector_name: Connector identifier.

        Returns:
            The connector instance, or None if not found.
        """
        return cls._connectors.get(connector_name)

    @classmethod
    def get_all(cls) -> list[BaseConnector]:
        """Return all registered connectors.

        Returns:
            List of all registered connector instances.
        """
        return list(cls._connectors.values())

    @classmethod
    def get_enabled(cls) -> list[BaseConnector]:
        """Return all enabled connectors sorted by priority.

        Returns:
            List of enabled connector instances sorted by priority (ascending).
        """
        enabled = [c for c in cls._connectors.values() if c.enabled]
        return sorted(enabled, key=lambda c: c.priority)

    @classmethod
    def clear(cls) -> None:
        """Clear all registered connectors (for testing).

        .. note:: Do not call in production code.
        """
        cls._connectors.clear()
        logger.info("Cleared all connectors from registry")
