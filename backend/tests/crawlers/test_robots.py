"""Tests for robots.txt compliance checker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.crawlers.robots import RobotsDirective, RobotsManager


class TestRobotsDirective:
    """Test RobotsDirective dataclass."""

    def test_allowed_directive(self):
        """Allowed directive has correct attributes."""
        directive = RobotsDirective(allowed=True)
        assert directive.allowed is True
        assert directive.reason == ""

    def test_disallowed_directive(self):
        """Disallowed directive has correct attributes."""
        directive = RobotsDirective(allowed=False, reason="Disallow /admin")
        assert directive.allowed is False
        assert directive.reason == "Disallow /admin"


class TestRobotsManager:
    """Test RobotsManager functionality."""

    @pytest.fixture
    def cache(self):
        """Create a mock cache for robots manager."""
        return MagicMock()

    @pytest.fixture
    def manager(self, cache):
        """Create RobotsManager instance."""
        return RobotsManager(cache=cache)

    @pytest.mark.asyncio
    async def test_is_allowed_no_rules(self, manager, cache):
        """URL is allowed when no robots.txt rules match."""
        cache.get.return_value = None
        with patch.object(manager, "_fetch_and_parse", new=AsyncMock()) as mock_parse:
            mock_parse.return_value = RobotsDirective(allowed=True)
            result = await manager.is_allowed("https://example.com/page")
            assert result.allowed is True

    @pytest.mark.asyncio
    async def test_is_allowed_from_cache(self, manager, cache):
        """Cached results are returned without re-fetching."""
        cached = RobotsDirective(allowed=True, reason="Cached")
        cache.get.return_value = cached
        with patch.object(manager, "_fetch_and_parse", new=AsyncMock()) as mock_parse:
            result = await manager.is_allowed("https://example.com/page")
            assert result.allowed is True
            cache.get.assert_called_once()
            mock_parse.assert_not_called()

    @pytest.mark.asyncio
    async def test_is_blocked_by_disallow(self, manager, cache):
        """URL is blocked when robots.txt disallows the path."""
        cache.get.return_value = None
        with patch.object(manager, "_fetch_and_parse", new=AsyncMock()) as mock_parse:
            mock_parse.return_value = RobotsDirective(
                allowed=False, reason="Disallowed by: /private"
            )
            result = await manager.is_allowed("https://example.com/private")
            assert result.allowed is False
            assert "private" in result.reason

    def test_path_matches_exact(self, manager):
        """Exact path matching works."""
        assert manager._path_matches("/admin", "/admin") is True

    def test_path_matches_prefix(self, manager):
        """Prefix path matching works."""
        assert manager._path_matches("/admin/settings", "/admin") is True
        assert manager._path_matches("/admin", "/admin/settings") is False

    def test_path_matches_wildcard(self, manager):
        """Wildcard path matching works."""
        assert manager._path_matches("/admin/*", "/admin/*") is True  # type: ignore[arg-type]
        assert manager._path_matches("/admin/page", "/admin/*") is True  # type: ignore[arg-type]

    def test_empty_disallow_allows(self, manager):
        """Empty disallow path means allow all."""
        assert manager._path_matches("/anything", "") is False

    @pytest.mark.asyncio
    async def test_different_hosts_cached_separately(self, manager, cache):
        """Different hosts have separate cache entries."""
        cache.get.side_effect = [None, None]
        with patch.object(manager, "_fetch_and_parse", new=AsyncMock()) as mock_parse:
            mock_parse.return_value = RobotsDirective(allowed=True)
            await manager.is_allowed("https://example1.com/page")
            await manager.is_allowed("https://example2.com/page")
            assert cache.get.call_count == 2
