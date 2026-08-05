"""Reusable HTTP client for connector requests.

Provides session-managed HTTP requests with configurable timeouts,
automatic retries, rate limiting, and standard headers.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.engines.source_connectors.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class HTTPClient:
    """Session-managed HTTP client for connector requests.

    Args:
        timeout: Request timeout in seconds.
        max_retries: Maximum number of retry attempts for transient failures.
        user_agent: User-Agent header sent with every request.
        rate_limit: Requests per second (0.0 = no rate limiting).
    """

    def __init__(
        self,
        timeout: int = 30,
        max_retries: int = 3,
        user_agent: str = "LeadHunterPro/1.0",
        rate_limit: float = 0.0,
    ) -> None:
        self._timeout = timeout
        self._user_agent = user_agent
        self._rate_limiter = RateLimiter(rate=rate_limit)
        self._session = self._create_session(max_retries)

    def _create_session(self, max_retries: int) -> requests.Session:
        """Create a Session with automatic retry strategy.

        Args:
            max_retries: Number of retries for eligible HTTP status codes.

        Returns:
            Configured requests.Session.
        """
        session = requests.Session()
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> requests.Response:
        """Execute a GET request with rate limiting and retries.

        Args:
            url: Target URL.
            params: Optional query parameters.
            headers: Optional extra headers.
            timeout: Optional per-request timeout override.

        Returns:
            The HTTP response.

        Raises:
            requests.HTTPError: If the response status indicates an error.
        """
        self._rate_limiter.acquire()
        default_headers = {
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/json,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        default_headers.update(headers or {})

        response = self._session.get(
            url,
            params=params,
            headers=default_headers,
            timeout=timeout or self._timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response

    def post(
        self,
        url: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> requests.Response:
        """Execute a POST request with rate limiting and retries.

        Args:
            url: Target URL.
            json: JSON body to send.
            headers: Optional extra headers.
            timeout: Optional per-request timeout override.

        Returns:
            The HTTP response.

        Raises:
            requests.HTTPError: If the response status indicates an error.
        """
        self._rate_limiter.acquire()
        default_headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        default_headers.update(headers or {})

        response = self._session.post(
            url,
            json=json,
            headers=default_headers,
            timeout=timeout or self._timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> "HTTPClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
