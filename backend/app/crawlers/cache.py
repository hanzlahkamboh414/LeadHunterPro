"""TTL-based in-memory response cache.

Provides a simple key-value cache for crawled responses, robots.txt
results, and domain health checks. The interface is designed to be
replaceable with a Redis backend without changing crawler code.

Usage:
    cache = ResponseCache()
    cache.set("key", value, ttl_seconds=600)
    result = cache.get("key")  # Returns None if expired or missing
"""

from __future__ import annotations

import logging
import time
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CacheEntry(Generic[T]):
    """Internal cache entry with expiration tracking."""

    __slots__ = ("expires_at", "value")

    def __init__(self, value: T, ttl_seconds: int) -> None:
        """Initialize a cache entry.

        Args:
            value: The cached value.
            ttl_seconds: Time-to-live in seconds.
        """
        self.value = value
        self.expires_at = time.monotonic() + ttl_seconds

    @property
    def is_expired(self) -> bool:
        """Check if this entry has expired."""
        return time.monotonic() > self.expires_at


class ResponseCache:
    """In-memory TTL cache for crawler results.

    This cache stores crawled HTTP responses, robots.txt parses,
    and domain health check results. It is keyed by URL or host
    and automatically expires entries after their TTL.

    The interface is deliberately simple so that a Redis-backed
    implementation can be swapped in later without changing
    caller code.
    """

    def __init__(self, default_ttl: int = 600) -> None:
        """Initialize the cache.

        Args:
            default_ttl: Default time-to-live in seconds for entries
                without an explicit TTL.
        """
        self._store: dict[str, CacheEntry[Any]] = {}
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def get(self, key: str, default: Any = None) -> Any | None:
        """Retrieve a value from the cache.

        Args:
            key: Cache key.
            default: Value to return if key is missing or expired.

        Returns:
            Cached value, or default if not found/expired.
        """
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return default
        if entry.is_expired:
            del self._store[key]
            self._misses += 1
            return default
        self._hits += 1
        return entry.value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """Store a value in the cache.

        Args:
            key: Cache key.
            value: Value to cache.
            ttl_seconds: Time-to-live in seconds. Uses default if None.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        self._store[key] = CacheEntry(value, ttl)

    def delete(self, key: str) -> bool:
        """Remove a specific key from the cache.

        Args:
            key: Cache key to remove.

        Returns:
            True if the key existed, False otherwise.
        """
        if key in self._store:
            del self._store[key]
            return True
        return False

    def clear(self) -> None:
        """Remove all entries from the cache."""
        self._store.clear()
        self._hits = 0
        self._misses = 0

    def cleanup(self) -> int:
        """Remove all expired entries.

        Returns:
            Number of entries removed.
        """
        expired_keys = [k for k, v in self._store.items() if v.is_expired]
        for key in expired_keys:
            del self._store[key]
        removed = len(expired_keys)
        if removed > 0:
            logger.debug("Cleaned up %d expired cache entries", removed)
        return removed

    @property
    def hit_rate(self) -> float:
        """Return the cache hit rate as a percentage."""
        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return (self._hits / total) * 100.0

    @property
    def size(self) -> int:
        """Return the number of non-expired entries."""
        self.cleanup()
        return len(self._store)
