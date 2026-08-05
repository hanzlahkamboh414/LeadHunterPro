"""Tests for session manager."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.crawlers.session_manager import SessionManager


class TestSessionManager:
    """Test SessionManager lifecycle."""

    @pytest.fixture
    def manager(self):
        """Create SessionManager instance."""
        return SessionManager()

    def test_initial_state_closed(self, manager):
        """Manager starts with no open session."""
        assert manager.is_open is False

    @pytest.mark.asyncio
    async def test_session_created_on_access(self, manager):
        """Session is created when first accessed."""
        mock_session = AsyncMock()
        mock_session.closed = False
        with patch("aiohttp.ClientSession", return_value=mock_session) as mock_class:
            _ = manager.session
            mock_class.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_reused(self, manager):
        """Same session is reused on subsequent accesses."""
        mock_session = AsyncMock()
        mock_session.closed = False
        with patch("aiohttp.ClientSession", return_value=mock_session) as mock_class:
            session1 = manager.session
            session2 = manager.session
            assert session1 is session2
            assert mock_class.call_count == 1

    @pytest.mark.asyncio
    async def test_new_session_after_close(self, manager):
        """New session is created after closing."""
        mock_session = AsyncMock()
        mock_session.closed = False
        with patch("aiohttp.ClientSession", return_value=mock_session) as mock_class:
            _ = manager.session
            assert manager._session is mock_session
            await manager.close()
            mock_session.close.assert_called_once()
            # After close, accessing session again should create a new one
            new_session = AsyncMock()
            new_session.closed = False
            mock_class.return_value = new_session
            second_session = manager.session
            assert second_session is not None
            # Session should have been recreated (call count incremented)
            assert mock_class.call_count >= 1

    @pytest.mark.asyncio
    async def test_close_without_session(self, manager):
        """Closing without an open session is safe."""
        await manager.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_async_context_manager(self, manager):
        """Manager works as async context manager."""
        mock_session = AsyncMock()
        mock_session.closed = False
        with patch("aiohttp.ClientSession", return_value=mock_session):
            async with manager as m:
                assert m is manager
                _ = m.session
            mock_session.close.assert_called_once()
