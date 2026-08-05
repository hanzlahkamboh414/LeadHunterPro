"""Tests for response cache."""

from __future__ import annotations

import time

from app.crawlers.cache import ResponseCache


class TestResponseCache:
    """Test ResponseCache TTL-based caching."""

    def test_set_and_get(self):
        """Basic set/get operations work."""
        cache = ResponseCache()
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_get_missing_key_returns_none(self):
        """Getting a missing key returns None."""
        cache = ResponseCache()
        assert cache.get("missing") is None

    def test_get_missing_key_with_default(self):
        """Getting a missing key returns default value."""
        cache = ResponseCache()
        assert cache.get("missing", "default") == "default"

    def test_overwrite_existing_key(self):
        """Setting a key overwrites the previous value."""
        cache = ResponseCache()
        cache.set("key1", "value1")
        cache.set("key1", "value2")
        assert cache.get("key1") == "value2"

    def test_delete_key(self):
        """Deleting a key removes it from cache."""
        cache = ResponseCache()
        cache.set("key1", "value1")
        assert cache.delete("key1") is True
        assert cache.get("key1") is None

    def test_delete_missing_key(self):
        """Deleting a missing key returns False."""
        cache = ResponseCache()
        assert cache.delete("missing") is False

    def test_clear_all(self):
        """Clearing removes all entries."""
        cache = ResponseCache()
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.clear()
        assert cache.get("key1") is None
        assert cache.get("key2") is None
        assert cache.size == 0

    def test_ttl_expiration(self):
        """Entries expire after their TTL."""
        cache = ResponseCache(default_ttl=1)
        cache.set("key1", "value1", ttl_seconds=1)
        assert cache.get("key1") == "value1"
        # Wait for expiration
        time.sleep(1.1)
        assert cache.get("key1") is None

    def test_cleanup_removes_expired(self):
        """Cleanup removes expired entries."""
        cache = ResponseCache(default_ttl=1)
        cache.set("key1", "value1", ttl_seconds=1)
        cache.set("key2", "value2", ttl_seconds=60)
        time.sleep(1.1)
        removed = cache.cleanup()
        assert removed == 1
        assert cache.get("key1") is None
        assert cache.get("key2") == "value2"

    def test_hit_rate_tracking(self):
        """Hit rate is calculated correctly."""
        cache = ResponseCache()
        cache.set("key1", "value1")
        cache.get("key1")  # hit
        cache.get("key1")  # hit
        cache.get("missing")  # miss
        # 2 hits out of 3 attempts = 66.67%
        assert abs(cache.hit_rate - 66.67) < 0.01

    def test_zero_hits_returns_zero_rate(self):
        """Hit rate is 0 when no requests made."""
        cache = ResponseCache()
        assert cache.hit_rate == 0.0

    def test_size_tracking(self):
        """Size reflects current non-expired entries."""
        cache = ResponseCache()
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        assert cache.size == 2
        cache.clear()
        assert cache.size == 0

    def test_different_keys_independent(self):
        """Different keys store independent values."""
        cache = ResponseCache()
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        assert cache.get("key1") == "value1"
        assert cache.get("key2") == "value2"

    def test_store_arbitrary_values(self):
        """Cache can store any serializable value."""
        cache = ResponseCache()
        cache.set("dict_key", {"a": 1, "b": 2})
        cache.set("list_key", [1, 2, 3])
        cache.set("int_key", 42)
        assert cache.get("dict_key") == {"a": 1, "b": 2}
        assert cache.get("list_key") == [1, 2, 3]
        assert cache.get("int_key") == 42
