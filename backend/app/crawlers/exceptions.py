"""Crawler-specific exception hierarchy.

All crawler errors inherit from CrawlerError, enabling connectore
to catch the broad category or handle individual error types
with precise granularity.
"""

from __future__ import annotations


class CrawlerError(Exception):
    """Base exception for all crawler-related errors."""


class CrawlerTimeout(CrawlerError):
    """Request exceeded the configured timeout duration."""


class CrawlerHTTPError(CrawlerError):
    """HTTP response indicated an error status (4xx/5xx).

    Attributes:
        status_code: The HTTP status code received.
        url: The URL that produced the error.
    """

    def __init__(self, status_code: int, url: str, message: str = "") -> None:
        """Initialize the HTTP error.

        Args:
            status_code: HTTP response status code.
            url: Request URL that failed.
            message: Optional human-readable description.
        """
        self.status_code = status_code
        self.url = url
        self.message = message or f"HTTP {status_code} from {url}"
        super().__init__(self.message)


class CrawlerRetryExceeded(CrawlerError):
    """All retry attempts were exhausted without success."""

    def __init__(self, url: str, last_error: Exception) -> None:
        """Initialize the retry-exhausted error.

        Args:
            url: The URL that could not be fetched after retries.
            last_error: The exception from the final attempt.
        """
        self.url = url
        self.last_error = last_error
        super().__init__(f"All retries exhausted for {url}. Last error: {last_error}")


class CrawlerRobotsBlocked(CrawlerError):
    """Request was blocked by robots.txt policy for the target host."""

    def __init__(self, url: str, reason: str = "Disallowed by robots.txt") -> None:
        """Initialize the robots-blocked error.

        Args:
            url: The URL that was blocked.
            reason: Explanation of why it was blocked.
        """
        self.url = url
        self.reason = reason
        super().__init__(f"Robots blocked {url}: {reason}")
