"""Data models for the search provider system.

Provides structured request/response types that all search providers
implement consistently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class SearchQuery:
    """A search query sent to a provider.

    Attributes:
        keywords: The search terms to query.
        location: Geographic location hint (e.g. 'Dallas, TX').
        num_results: Maximum results to return (provider-dependent).
        safe_search: Whether to filter explicit content.
    """

    keywords: str
    location: str = ""
    num_results: int = 10
    safe_search: bool = True


@dataclass(frozen=True)
class SearchResult:
    """A single search result from any provider.

    Attributes:
        title: Page title.
        url: Absolute URL of the result.
        snippet: Short description text.
        domain: Extracted hostname (lowercase, no www.).
        position: Rank position in results (1-based).
    """

    title: str
    url: str
    snippet: str = ""
    domain: str = ""
    position: int = 0

    def __post_init__(self) -> None:
        """Normalize domain after construction."""
        if not self.domain and self.url:
            from urllib.parse import urlparse

            parsed = urlparse(self.url)
            object.__setattr__(
                self,
                "domain",
                (parsed.hostname or "").lower().replace("www.", "").strip(),
            )


@dataclass
class SearchResponse:
    """Aggregated response from a search provider.

    Attributes:
        results: List of matching search results.
        provider: Name of the provider that produced these results.
        query: The original query that was executed.
        total_estimated: Estimated total matches (provider-dependent).
        latency_ms: Time taken to fetch results.
        error: Error message if the request failed, else empty string.
        status: 'success' | 'error' | 'partial'.
    """

    results: list[SearchResult] = field(default_factory=list)
    provider: str = ""
    query: str = ""
    total_estimated: int = 0
    latency_ms: float = 0.0
    error: str = ""
    status: Literal["success", "error", "partial"] = "success"


# ---------------------------------------------------------------------------
# Type aliases for convenience
# ---------------------------------------------------------------------------

SearchResults = list[SearchResult]
"""Alias for a list of search results."""
