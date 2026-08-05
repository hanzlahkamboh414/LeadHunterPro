"""Low-level HTTP session manager for the crawler.

Manages aiohttp ClientSession instances with connection pooling,
TLS verification, and proper lifecycle management. Connectors
should use HTTPCrawler (which wraps this) rather than using
this class directly.
"""

from __future__ import annotations

import logging
from typing import Any, Self

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages aiohttp ClientSession lifecycle and pooling.

    Ensures sessions are created once and reused, with proper
    cleanup on shutdown. Prevents connection leaks.
    """

    def __init__(self) -> None:
        """Initialize the session manager."""
        self._session: Any = None

    @property
    def session(self) -> Any:
        """Get or create the aiohttp client session.

        Returns:
            The aiohttp ClientSession instance.
        """
        if self._session is None or self._session.closed:
            import aiohttp

            self._session = aiohttp.ClientSession(
                timer=None,
                auto_decompress=False,
                trust_env=True,
            )
        return self._session

    @property
    def is_open(self) -> bool:
        """Check if the session is currently open."""
        return self._session is not None and not self._session.closed

    async def close(self) -> None:
        """Close the session and release all connections."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            logger.debug("HTTP session closed")

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        """Async context manager exit."""
        await self.close()
