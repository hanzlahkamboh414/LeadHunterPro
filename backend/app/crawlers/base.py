"""Base crawler interface and request model.

Defines the abstract interface that all crawlers must implement,
and the immutable CrawlRequest dataclass that controls each crawl.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from app.crawlers.response import CrawlResponse


@dataclass(frozen=True)
class CrawlRequest:
    """Immutable configuration for a single crawl request.

    Attributes:
        url: The full URL to crawl.
        method: HTTP method (GET, POST, etc.).
        headers: Additional HTTP headers to send.
        timeout: Request timeout in seconds.
        follow_redirects: Whether to follow HTTP redirects.
        respect_robots: Whether to check robots.txt first.
        session_id: Optional session identifier for connection reuse.
    """

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    timeout: int = 30
    follow_redirects: bool = True
    respect_robots: bool = True
    session_id: str | None = None


class BaseCrawler(ABC):
    """Abstract base class for all crawlers.

    Every concrete crawler must implement:
    - crawl(): Execute a single crawl request
    - close(): Clean up resources

    The base class provides default implementations for lifecycle
    management that subclasses can override.
    """

    @abstractmethod
    async def crawl(self, request: CrawlRequest) -> CrawlResponse:
        """Execute a crawl request and return the response.

        Args:
            request: The crawl request configuration.

        Returns:
            CrawlResponse with the result.

        Raises:
            CrawlerError: If the crawl fails after retries.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources (close sessions, release connections)."""
        ...

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        """Async context manager exit."""
        await self.close()
