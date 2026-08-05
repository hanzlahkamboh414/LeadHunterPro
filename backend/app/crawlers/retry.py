"""Exponential backoff retry engine.

Wraps callable executions with automatic retry logic using
exponential backoff. Retries only on transient failures
(connection errors, timeouts, 5xx responses).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from app.crawlers.exceptions import CrawlerRetryExceeded

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def connector_retry(
    max_retries: int = 3,
    backoff_base: float = 2.0,
    backoff_max: float = 30.0,
    retry_on: tuple[type[Exception], ...] | None = None,
) -> Callable[[F], F]:
    """Decorator that adds exponential backoff retry to a function.

    Args:
        max_retries: Maximum number of retry attempts.
        backoff_base: Base delay in seconds for exponential backoff.
        backoff_max: Maximum delay cap in seconds.
        retry_on: Tuple of exception types to retry on.
            Defaults to (ConnectionError, TimeoutError, OSError).

    Returns:
        Wrapped function with retry logic.

    Example:
        @connector_retry(max_retries=3, backoff_base=1.0)
        async def fetch_page(url: str) -> str:
            ...
    """
    if retry_on is None:
        retry_on = (ConnectionError, TimeoutError, OSError)

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_error: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retry_on as exc:
                    last_error = exc
                    if attempt < max_retries:
                        delay = min(backoff_base**attempt, backoff_max)
                        logger.warning(
                            "Attempt %d/%d failed for %s: %s. Retrying in %.1fs...",
                            attempt + 1,
                            max_retries + 1,
                            func.__name__,
                            exc,
                            delay,
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            "All %d retries exhausted for %s: %s",
                            max_retries + 1,
                            func.__name__,
                            exc,
                        )
            raise CrawlerRetryExceeded(
                url=kwargs.get("url", str(args[0]) if args else "unknown"),
                last_error=last_error or Exception("Unknown error"),
            )

        return wrapper  # type: ignore[return-value]

    return decorator


class RetryEngine:
    """Programmatic retry engine for complex retry scenarios.

    Use this class when you need more control over retry logic
    than the decorator provides (e.g., conditional retries,
    custom backoff strategies).
    """

    def __init__(
        self,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        backoff_max: float = 30.0,
        retry_on: tuple[type[Exception], ...] | None = None,
    ) -> None:
        """Initialize the retry engine.

        Args:
            max_retries: Maximum number of retry attempts.
            backoff_base: Base delay in seconds.
            backoff_max: Maximum delay cap in seconds.
            retry_on: Exception types to retry on.
        """
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._retry_on = retry_on or (ConnectionError, TimeoutError, OSError)

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute a function with retry logic.

        Args:
            func: Callable to execute.
            *args: Positional arguments for the callable.
            **kwargs: Keyword arguments for the callable.

        Returns:
            The return value of the callable.

        Raises:
            CrawlerRetryExceeded: If all retries are exhausted.
        """
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                return func(*args, **kwargs)
            except self._retry_on as exc:
                last_error = exc
                if attempt < self._max_retries:
                    delay = min(self._backoff_base**attempt, self._backoff_max)
                    url = kwargs.get("url", "unknown")
                    logger.warning(
                        "Attempt %d/%d failed for %s: %s. Retrying in %.1fs...",
                        attempt + 1,
                        self._max_retries + 1,
                        url,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "All %d retries exhausted for %s: %s",
                        self._max_retries + 1,
                        kwargs.get("url", "unknown"),
                        exc,
                    )

        raise CrawlerRetryExceeded(
            url=kwargs.get("url", "unknown"),
            last_error=last_error or Exception("Unknown error"),
        )

    def should_retry(self, exception: Exception) -> bool:
        """Check if an exception warrants a retry.

        Args:
            exception: The exception to evaluate.

        Returns:
            True if the exception is in the retry-on list.
        """
        return isinstance(exception, self._retry_on)
