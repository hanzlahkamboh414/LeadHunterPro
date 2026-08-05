"""Base connector interface for all source connectors.

Every connector must inherit from this class and implement the
required methods. The connector framework handles registration,
discovery orchestration, and result normalization.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.connectors.connector_result import ConnectorResult


class BaseConnector(ABC):
    """Abstract base class for all source connectors.

    Every connector MUST:
    1. Inherit from this class.
    2. Set the class attributes: connector_name, priority, enabled.
    3. Implement the abstract methods: search, health_check, validate_result.
    """

    #: Unique identifier for this connector (e.g. 'texas_procurement').
    connector_name: str = ""

    #: Discovery priority (lower number = higher priority, checked first).
    priority: int = 100

    #: Whether this connector is enabled for discovery.
    enabled: bool = True

    @abstractmethod
    def search(
        self,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[ConnectorResult], dict[str, Any]]:
        """Execute discovery and return companies.

        Args:
            industry: Industry keyword (e.g. 'Construction Estimating').
            location: Geographic location (e.g. 'Dallas Texas USA').
            limit: Maximum companies to return.

        Returns:
            Tuple of (list of ConnectorResult, metadata dict).
        """
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """Check if this connector can operate in the current environment.

        Returns:
            True if the connector is available and healthy.
        """
        ...

    @abstractmethod
    def validate_result(self, result: ConnectorResult) -> bool:
        """Validate a single connector result.

        Args:
            result: The connector result to validate.

        Returns:
            True if the result passes validation.
        """
        ...

    def get_metadata(self) -> dict[str, Any]:
        """Return metadata about this connector.

        Returns:
            Dictionary with connector metadata.
        """
        return {
            "connector_name": self.connector_name,
            "priority": self.priority,
            "enabled": self.enabled,
            "health": self.health_check(),
        }
