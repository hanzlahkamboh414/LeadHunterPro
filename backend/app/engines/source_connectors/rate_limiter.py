"""Rate limiter for connector HTTP requests.

Enforces a maximum requests-per-second limit with a token-bucket algorithm.
"""

from __future__ import annotations

import time
from threading import Lock


class RateLimiter:
    """Token-bucket rate limiter for outgoing HTTP requests.

    Args:
        rate: Maximum requests per second. Use ``0.0`` to disable limiting.
    """

    def __init__(self, rate: float = 0.0) -> None:
        self._rate = rate
        self._lock = Lock()
        self._last_request_time: float = 0.0

    def acquire(self) -> None:
        """Block until a request is allowed under the current rate limit.

        If rate limiting is disabled (rate == 0.0), this is a no-op.
        """
        if self._rate <= 0.0:
            return

        with self._lock:
            min_interval = 1.0 / self._rate
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._last_request_time = time.monotonic()

    @property
    def rate(self) -> float:
        """Current rate limit in requests per second."""
        return self._rate
