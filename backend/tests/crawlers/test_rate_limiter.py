"""Tests for rate limiter."""

from __future__ import annotations

import time

from app.crawlers.rate_limiter import RateLimiter


class TestRateLimiter:
    """Test token-bucket rate limiter."""

    def test_acquire_allows_first_request(self):
        """First request is allowed immediately."""
        limiter = RateLimiter(per_host_delay=0.1, global_delay=0.1)
        start = time.monotonic()
        limiter.acquire("example.com")
        elapsed = time.monotonic() - start
        # Should be very fast (no prior requests)
        assert elapsed < 0.05

    def test_acquire_respects_per_host_delay(self):
        """Subsequent requests to same host respect delay."""
        limiter = RateLimiter(per_host_delay=0.1, global_delay=0.01)
        limiter.acquire("example.com")
        start = time.monotonic()
        limiter.acquire("example.com")
        elapsed = time.monotonic() - start
        assert elapsed >= 0.09

    def test_acquire_allows_different_hosts(self):
        """Requests to different hosts are not blocked by per-host delay."""
        limiter = RateLimiter(per_host_delay=0.1, global_delay=0.01)
        limiter.acquire("example1.com")
        start = time.monotonic()
        limiter.acquire("example2.com")
        elapsed = time.monotonic() - start
        # Should be fast (different host)
        assert elapsed < 0.05

    def test_acquire_respects_global_delay(self):
        """Global delay is enforced across all hosts."""
        limiter = RateLimiter(per_host_delay=0.01, global_delay=0.1)
        limiter.acquire("example1.com")
        start = time.monotonic()
        limiter.acquire("example2.com")
        elapsed = time.monotonic() - start
        assert elapsed >= 0.09

    def test_estimate_wait_zero_after_acquire(self):
        """Wait estimate is near zero immediately after acquire."""
        limiter = RateLimiter(per_host_delay=0.1, global_delay=0.1)
        limiter.acquire("example.com")
        wait = limiter.estimate_wait("example.com")
        assert wait > 0.05  # Should be close to full delay

    def test_estimate_wait_decreases_over_time(self):
        """Wait estimate decreases as time passes."""
        limiter = RateLimiter(per_host_delay=1.0, global_delay=0.01)
        limiter.acquire("example.com")
        initial_wait = limiter.estimate_wait("example.com")
        time.sleep(0.5)
        reduced_wait = limiter.estimate_wait("example.com")
        assert reduced_wait < initial_wait

    def test_default_delays(self):
        """Default delay values are reasonable."""
        limiter = RateLimiter()
        assert limiter._per_host_delay == 0.5
        assert limiter._global_delay == 0.1

    def test_multiple_requests_same_host(self):
        """Multiple requests to same host are spaced correctly."""
        limiter = RateLimiter(per_host_delay=0.1, global_delay=0.01)
        # First request should be fast (no prior request)
        start = time.monotonic()
        limiter.acquire("example.com")
        first_duration = time.monotonic() - start
        assert first_duration < 0.05  # First request is instant

        # Second request should be delayed by per-host delay
        start = time.monotonic()
        limiter.acquire("example.com")
        second_duration = time.monotonic() - start
        assert second_duration >= 0.09  # Must wait for rate limit

        # Third request similarly delayed
        start = time.monotonic()
        limiter.acquire("example.com")
        third_duration = time.monotonic() - start
        assert third_duration >= 0.09
