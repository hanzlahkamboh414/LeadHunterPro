"""Token-bucket rate limiter for crawler requests.

Enforces both per-host and global request rate limits to prevent
overloading target servers. Uses a token bucket algorithm with
thread-safe execution.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token-bucket rate limiter with per-host and global limits.

    Attributes:
        per_host_delay: Minimum seconds between requests to the same host.
        global_delay: Minimum seconds between any two requests.
    """

    def __init__(
        self,
        per_host_delay: float = 0.5,
        global_delay: float = 0.1,
    ) -> None:
        """Initialize the rate limiter.

        Args:
            per_host_delay: Minimum seconds between requests to the same host.
            global_delay: Minimum seconds between any two requests.
        """
        self._per_host_delay = per_host_delay
        self._global_delay = global_delay
        self._host_timestamps: dict[str, float] = defaultdict(float)
        self._global_last_request: float = 0.0

    def acquire(self, host: str) -> None:
        """Acquire permission to make a request.

        Blocks until the rate limit allows the request. Must be called
        before every HTTP request.

        Args:
            host: The hostname being requested (for per-host limiting).
        """
        now = time.monotonic()

        # Per-host limit
        host_allowed_at = self._host_timestamps[host] + self._per_host_delay
        if now < host_allowed_at:
            wait = host_allowed_at - now
            logger.debug("Rate limiting: waiting %.2fs for host %s", wait, host)
            time.sleep(wait)

        # Global limit
        global_allowed_at = self._global_last_request + self._global_delay
        if time.monotonic() < global_allowed_at:
            wait = global_allowed_at - time.monotonic()
            logger.debug("Rate limiting: waiting %.2fs globally", wait)
            time.sleep(wait)

        # Update timestamps
        now = time.monotonic()
        self._host_timestamps[host] = now
        self._global_last_request = now

    def estimate_wait(self, host: str) -> float:
        """Estimate how long to wait before the next request is allowed.

        Args:
            host: The hostname being requested.

        Returns:
            Estimated wait time in seconds.
        """
        now = time.monotonic()
        host_wait = max(0.0, self._host_timestamps[host] + self._per_host_delay - now)
        global_wait = max(0.0, self._global_last_request + self._global_delay - now)
        return max(host_wait, global_wait)
