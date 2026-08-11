"""Company discovery data models.

This module defines the immutable result structures used by the company
discovery pipeline (search → validate → clean).  No database writes occur
here; results are plain dataclasses suitable for API responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SUPPORTED_SOURCES = Literal[
    "serper",
    "serpapi",
    "google_cse",
    "bing",
    "duckduckgo",
    "directory",
    "government",
    "construction_directory",
]


@dataclass(frozen=True)
class CompanyDiscoveryResult:
    """A single discovered company candidate."""

    company_name: str
    website: str
    city: str = ""
    state: str = ""
    country: str = "USA"
    source: SUPPORTED_SOURCES = "google"
    confidence: float = 0.5
    source_url: str = ""
    discovery_reason: str = ""
    # Phase 3 Step 4: additive passthrough of the connector's per-record
    # metadata (deterministic ``verification`` + additive ``ai`` /
    # ``qualification``) so it reaches the discovery API output. Never
    # recomputed here — carried verbatim from the source ConnectorResult.
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def normalized_website(self) -> str:
        """Return the website without trailing slash and with https prefix."""
        url = self.website.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url


@dataclass
class DiscoveryMetrics:
    """Mutable metrics accumulated during a discovery run."""

    total_found: int = 0
    total_validated: int = 0
    total_cleaned: int = 0
    errors: list[str] = field(default_factory=list)
