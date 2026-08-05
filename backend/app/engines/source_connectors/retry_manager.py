"""Retry manager with exponential backoff.

Provides both a class-based API and a decorator for adding retry logic
to connector methods that make external HTTP requests.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)


class RetryManager:
    """Manages retry attempts with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts (not counting the
            initial attempt).
        backoff_factor: Multiplier applied to the wait time after each
            failed attempt. Wait time = ``backoff_factor * (2 ** attempt)``.
        exceptions: Tuple of exception types that trigger a retry.
    """

    def __init__(
        self,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        exceptions: tuple[type[Exception], ...] = (requests.RequestException,),
    ) -> None:
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._exceptions = exceptions

    def execute(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute *func* with retry logic.

        Args:
            func: Callable to execute.
            *args: Positional arguments passed to *func*.
            **kwargs: Keyword arguments passed to *func*.

        Returns:
            The return value of *func*.

        Raises:
            The last exception raised by *func* if all retries are exhausted.
        """
        last_exception: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return func(*args, **kwargs)
            except self._exceptions as exc:
                last_exception = exc
                if attempt < self._max_retries:
                    wait = self._backoff_factor * (2 ** attempt)
                    logger.warning(
                        "%s attempt %d/%d failed: %s. Retrying in %.1fs...",
                        func.__name__,
                        attempt + 1,
                        self._max_retries + 1,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
        raise last_exception  # type: ignore[misc]

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator form: apply retry logic to a function.

        Args:
            func: Function to wrap.

        Returns:
            Wrapped function with retry logic.
        """
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return self.execute(func, *args, **kwargs)
        return wrapper


def connector_retry(
    func: Callable[..., Any] | None = None,
    *,
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    exceptions: tuple[type[Exception], ...] = (requests.RequestException,),
) -> Callable[..., Any] | Callable:
    """Decorator that adds exponential-backoff retry logic.

    Can be used with or without parentheses:

    .. code-block:: python

        @connector_retry
        def fetch(): ...

        @connector_retry(max_retries=5)
        def fetch(): ...
    """
    manager = RetryManager(
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        exceptions=exceptions,
    )
    if func is not None:
        return manager(func)
    return manager.__call__
