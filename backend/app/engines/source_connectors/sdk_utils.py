"""Connector SDK – utilities and helpers.

Provides reusable components used by all connectors:
    - ConnectorLogger: Structured logging wrapper
    - ConnectorRetry: Retry decorator with backoff
    - ConnectorErrorHandler: Centralized error handling
    - HTTPClient: Reusable HTTP client with session management
"""

from __future__ import annotations

import logging
import time
import functools
from typing import Any, Callable
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------


class ConnectorLogger:
    """Structured logger for connector operations.

    All log entries include the connector name as part of the logger path.
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(f"connectors.{name}")

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an informational message."""
        self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a warning."""
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, exc: Exception | None = None, *args: Any, **kwargs: Any) -> None:
        """Log an error, optionally with exception details."""
        if exc:
            self._logger.error(f"{msg}: {exc}", *args, exc_info=True, **kwargs)
        else:
            self._logger.error(msg, *args, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a debug message."""
        self._logger.debug(msg, *args, **kwargs)


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------


def connector_retry(
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    exceptions: tuple[type[Exception], ...] = (requests.RequestException,),
) -> Callable:
    """Decorator that adds retry logic to connector methods.

    Args:
        max_retries: Maximum number of retry attempts.
        backoff_factor: Multiplier for exponential backoff.
        exceptions: Tuple of exception types to retry on.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exception = exc
                    if attempt < max_retries:
                        wait = backoff_factor * (2 ** attempt)
                        logger.warning(
                            "%s attempt %d/%d failed: %s. Retrying in %.1fs...",
                            func.__name__, attempt + 1, max_retries + 1, exc, wait,
                        )
                        time.sleep(wait)
            raise last_exception  # type: ignore[misc]
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


class ConnectorErrorHandler:
    """Centralized error handling for connectors.

    Collects errors during discovery and provides structured feedback.
    """

    def __init__(self, connector_name: str) -> None:
        self._name = connector_name
        self._errors: list[dict[str, str]] = []
        self._warnings: list[str] = []

    def add_error(self, operation: str, message: str) -> None:
        """Record an error with its operation context."""
        self._errors.append({"operation": operation, "message": message})
        logger.error("[%s] Error in %s: %s", self._name, operation, message)

    def add_warning(self, message: str) -> None:
        """Record a non-fatal warning."""
        self._warnings.append(message)
        logger.warning("[%s] %s", self._name, message)

    @property
    def has_errors(self) -> bool:
        """Return True if any errors were recorded."""
        return len(self._errors) > 0

    @property
    def errors(self) -> list[dict[str, str]]:
        """Return all recorded errors."""
        return self._errors.copy()

    @property
    def warnings(self) -> list[str]:
        """Return all recorded warnings."""
        return self._warnings.copy()

    def summary(self) -> dict[str, Any]:
        """Return a summary of all errors and warnings."""
        return {
            "connector": self._name,
            "error_count": len(self._errors),
            "warning_count": len(self._warnings),
            "errors": self._errors,
            "warnings": self._warnings,
        }


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------


class HTTPClient:
    """Reusable HTTP client with retry and session management.

    Provides a consistent interface for making HTTP requests with:
    - Configurable timeouts
    - Automatic retries on failure
    - Session pooling
    - Standard headers
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
        self._rate_limit = rate_limit
        self._session = self._create_session(max_retries)
        self._last_request_time: float = 0.0

    def _create_session(self, max_retries: int) -> requests.Session:
        """Create a session with retry strategy."""
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

    def _enforce_rate_limit(self) -> None:
        """Sleep if rate limiting is configured."""
        if self._rate_limit > 0:
            min_interval = 1.0 / self._rate_limit
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> requests.Response:
        """Execute a GET request with rate limiting and retries."""
        self._enforce_rate_limit()
        self._last_request_time = time.monotonic()

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

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> "HTTPClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
