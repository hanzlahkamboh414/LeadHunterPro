"""Connector SDK – core models and interfaces.

This module defines the foundational types and base classes that every
connector must implement. All connectors inherit from ``BaseConnector``
and return ``CompanyResult`` instances.

Components:
    - CompanyResult: Standardized output model
    - ConnectorConfig: Configuration container
    - BaseConnector: Abstract base class
    - ConnectorRegistry: Singleton registry for all connectors
    - get_connector / list_connectors: Convenience accessors
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from typing import Any

from app.engines.source_connectors.sdk_utils import ConnectorLogger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompanyResult:
    """Standardized company record returned by all connectors."""

    company_name: str
    website: str
    city: str
    state: str
    country: str = "USA"
    source_url: str = ""
    industry_focus: str = ""
    revenue_tier: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompanyResult":
        """Create a CompanyResult from a raw dictionary.

        Args:
            data: Raw company data dictionary.

        Returns:
            A frozen CompanyResult instance.
        """
        return cls(
            company_name=data.get("company_name", ""),
            website=data.get("website", ""),
            city=data.get("city", ""),
            state=data.get("state", ""),
            country=data.get("country", "USA"),
            source_url=data.get("source_url", ""),
            industry_focus=data.get("industry_focus", ""),
            revenue_tier=data.get("revenue_tier", ""),
            extra={k: v for k, v in data.items() if k not in {
                "company_name", "website", "city", "state", "country",
                "source_url", "industry_focus", "revenue_tier",
            }},
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary."""
        return {
            "company_name": self.company_name,
            "website": self.website,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "source_url": self.source_url,
            "industry_focus": self.industry_focus,
            "revenue_tier": self.revenue_tier,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ConnectorConfig:
    """Configuration for a connector instance.

    Attributes:
        name: Connector identifier (e.g. 'texas_procurement').
        timeout: HTTP request timeout in seconds.
        max_retries: Number of retry attempts on failure.
        user_agent: User-Agent header for HTTP requests.
        rate_limit: Requests per second (0 = no limit).
        auth: Optional authentication dict (provider-specific).
    """

    name: str
    timeout: int = 30
    max_retries: int = 3
    user_agent: str = "LeadHunterPro/1.0 (contact@leadhunterpro.ai)"
    rate_limit: float = 0.0
    auth: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Base connector interface
# ---------------------------------------------------------------------------


class BaseConnector(ABC):
    """Abstract base class for all source connectors.

    Every connector MUST:
    1. Inherit from this class.
    2. Implement the ``discover()`` method.
    3. Return a list of CompanyResult instances.
    4. Never fabricate data — only return verified companies.
    """

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        """Initialize the connector.

        Args:
            config: Optional configuration. If None, defaults are used.
        """
        self._config = config or ConnectorConfig(name=self.__class__.__name__)
        self._logger = ConnectorLogger(name=self._config.name)

    @property
    def name(self) -> str:
        """Human-readable connector name."""
        return self._config.name

    @property
    def description(self) -> str:
        """One-line description of what this connector discovers."""
        return f"{self.__class__.__name__} connector"

    def is_available(self) -> bool:
        """Return True if this connector can run in the current environment."""
        return True

    @abstractmethod
    def discover(
        self,
        *,
        state: str | None = None,
        city: str | None = None,
        industry: str = "Construction Estimating",
        limit: int = 50,
    ) -> tuple[list[CompanyResult], dict[str, Any]]:
        """Execute discovery and return (companies, metadata).

        Args:
            state: US state code (e.g. 'TX').
            city: City name.
            industry: Industry filter.
            limit: Maximum companies to return.

        Returns:
            Tuple of (list of CompanyResult, metadata dict).
        """
        raise NotImplementedError(f"{self.__class__.__name__}.discover() not implemented")

    def _log(self, level: str, msg: str, **kwargs: Any) -> None:
        """Log a message through the connector logger."""
        getattr(self._logger, level)(msg, **kwargs)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ConnectorRegistry:
    """Singleton registry for all connectors."""

    _connectors: dict[str, BaseConnector] = {}
    _builtins_registered: bool = False

    @classmethod
    def register(cls, connector: BaseConnector) -> None:
        """Register a connector instance."""
        cls._connectors[connector.name] = connector
        logger.info("Registered connector: %s (%s)", connector.name, connector.description)

    @classmethod
    def get(cls, name: str) -> BaseConnector | None:
        """Get a connector by name."""
        return cls._connectors.get(name)

    @classmethod
    def list_all(cls) -> list[BaseConnector]:
        """Return all registered connectors."""
        return list(cls._connectors.values())

    @classmethod
    def list_names(cls) -> list[str]:
        """Return names of all registered connectors."""
        return list(cls._connectors.keys())

    @classmethod
    def clear(cls) -> None:
        """Clear all registered connectors (for testing).

        .. note:: Do not call in production code.
        """
        cls._connectors.clear()
        # Re-register builtins after clear so tests start with defaults
        cls._builtins_registered = False
        _register_builtin_connectors()


# Backward-compatibility alias
ConstructionSourceRegistry = ConnectorRegistry  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


def get_connector(name: str) -> BaseConnector | None:
    """Convenience function to look up a connector by name.

    Args:
        name: Connector identifier.

    Returns:
        The connector instance, or None if not found.
    """
    return ConnectorRegistry.get(name)


def list_connectors() -> list[dict[str, str]]:
    """Return a summary of all registered connectors.

    Returns:
        List of dicts with 'name' and 'description' keys.
    """
    return [
        {"name": c.name, "description": c.description}
        for c in ConnectorRegistry.list_all()
    ]


# Register built-in connectors here
# ConnectorRegistry.register(MyNewConnector())


# ---------------------------------------------------------------------------
# Built-in connector registration
# ---------------------------------------------------------------------------
# These are imported lazily to avoid circular dependencies.
# texas_procurement imports from base.py, so we register explicitly here.


def _register_builtin_connectors() -> None:
    """Register all built-in connectors at module load time."""
    from app.engines.source_connectors.texas_procurement import (  # noqa: PLC0415
        TexasProcurementConnector,
    )
    from app.engines.source_connectors.agc_texas import AgcTexasConnector  # noqa: PLC0415

    # Only register if not already present
    if "texas_procurement" not in ConnectorRegistry._connectors:
        ConnectorRegistry.register(TexasProcurementConnector())
    if "agc_texas" not in ConnectorRegistry._connectors:
        ConnectorRegistry.register(AgcTexasConnector())


# Trigger registration immediately
_register_builtin_connectors()
