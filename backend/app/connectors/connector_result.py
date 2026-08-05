"""Connector result model.

Defines the standardized output format for all connectors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConnectorResult:
    """A single company record discovered by a connector.

    Attributes:
        company_name: Legal company name.
        website: Official company website URL.
        city: City location.
        state: State code.
        country: Country code (default: "USA").
        source: Source identifier (e.g. 'texas_procurement', 'agc_texas').
        source_url: Original URL where the company was found.
        confidence: Confidence score in [0.0, 1.0].
        metadata: Additional provider-specific metadata.
    """

    company_name: str
    website: str
    city: str
    state: str
    country: str = "USA"
    source: str = ""
    source_url: str = ""
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectorResult:
        """Create a ConnectorResult from a raw dictionary.

        Args:
            data: Raw company data dictionary.

        Returns:
            A new ConnectorResult instance.
        """
        known_keys = {
            "company_name",
            "website",
            "city",
            "state",
            "country",
            "source",
            "source_url",
            "confidence",
            "metadata",
        }
        metadata = {k: v for k, v in data.items() if k not in known_keys}
        return cls(
            company_name=data.get("company_name", ""),
            website=data.get("website", ""),
            city=data.get("city", ""),
            state=data.get("state", ""),
            country=data.get("country", "USA"),
            source=data.get("source", ""),
            source_url=data.get("source_url", ""),
            confidence=float(data.get("confidence", 1.0)),
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary."""
        return {
            "company_name": self.company_name,
            "website": self.website,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "source": self.source,
            "source_url": self.source_url,
            "confidence": self.confidence,
            **self.metadata,
        }
