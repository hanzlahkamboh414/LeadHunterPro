"""Base interface for all search providers.

Every search provider must implement this abstract base class.
Providers can query public search engines (SearXNG, Brave) or
custom APIs.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.search_providers.models import SearchQuery, SearchResponse

logger = logging.getLogger(__name__)


class BaseSearchProvider(ABC):
    """Abstract base class for search providers.

    All providers must implement :meth:`search` which performs an
    actual web query and returns structured results.

    Subclasses are responsible for their own rate limiting, retries,
    and error handling — the manager only coordinates fallbacks.

    Attributes:
        provider_name: Unique identifier used by the provider registry.
        description: Human-readable one-liner for logs and diagnostics.
        enabled: Whether this provider is active (controlled by config).
        priority: Execution order among providers (lower = tried first).
    """

    provider_name: str = ""
    description: str = ""
    enabled: bool = True
    priority: int = 100
    # Per-query timeout. Providers enforce it THEMSELVES via aiohttp's
    # ClientTimeout (a clean internal timeout → honest error response, connector
    # untouched). SearchProviderManager applies it only as a BACKSTOP
    # (timeout_s + grace) for providers without an internal timeout — it must
    # never hard-cancel a real request at the same instant aiohttp's own
    # timeout fires, or the shared session connector is poisoned
    # ("Connector is closed").
    timeout_s: float = 10.0

    @abstractmethod
    async def search(self, query: SearchQuery) -> SearchResponse:
        """Execute a search query against this provider.

        Args:
            query: The search query to execute.

        Returns:
            A SearchResponse containing results and metadata.

        Raises:
            Does NOT raise — all errors are captured in SearchResponse.error.
        """
        ...

    async def health_check(self) -> dict[str, Any]:
        """Lightweight health check for this provider.

        Returns:
            Dict with 'healthy' (bool) and optional diagnostic info.
        """
        return {"healthy": self.enabled, "provider": self.provider_name}

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__}"
            f" name={self.provider_name!r}"
            f" enabled={self.enabled}>"
        )


class LoopSessionMixin:
    """One aiohttp ClientSession per event loop — the concurrency root fix.

    aiohttp sessions are LOOP-bound: a connection pool created inside one
    event loop cannot serve requests driven by another. The lead pipeline
    runs several CONCURRENT loops that all share the singleton provider
    instances from the registry — one ``asyncio.run`` per discovery pass
    (``SearchProviderSource``), one ``asyncio.gather`` batch per research
    thread (``RegistryIndexedSearch.search_many``). A single instance-level
    session is therefore a cross-loop race:

    * thread B's lazy ``_get_session`` overwrites the session thread A is
      mid-request on → aiohttp "Timeout context manager should be used
      inside a task";
    * thread B's ``close()`` (after its batch) closes A's in-flight
      connector → "Connector is closed".

    Live proof 2026-09-11: ~a third of lead-research company-info searches
    died to these two errors during a 3-thread run. Keying sessions by the
    RUNNING loop gives every loop its own pool — no sharing, no races.
    ``close()`` closes only the CALLING loop's session (every call site
    already closes inside the loop that used it). Entries whose loop has
    since closed are pruned on the next ``_get_session`` — their
    connections died with the loop, only the dict entry lingers.

    Mixin contract: the host provider defines ``self._timeout`` (seconds)
    and calls :meth:`_init_sessions` in its constructor.
    """

    def _init_sessions(self) -> None:
        #: id(running loop) -> (loop, session); the loop is kept so dead
        #: entries can be pruned via ``loop.is_closed()`` (an id alone
        #: can be reused by a new loop object).
        self._sessions: dict[int, tuple[Any, Any]] = {}

    async def _get_session(self) -> Any:
        """The session bound to the CURRENT event loop (created on demand)."""
        import asyncio

        import aiohttp

        loop = asyncio.get_running_loop()
        entry = self._sessions.get(id(loop))
        if entry is not None and not entry[0].is_closed() and not entry[1].closed:
            return entry[1]
        # Prune dead entries (loop closed or session closed) before adding.
        self._sessions = {
            k: v for k, v in self._sessions.items()
            if not v[0].is_closed() and not v[1].closed
        }
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout)
        )
        self._sessions[id(loop)] = (loop, session)
        return session

    async def close(self) -> None:
        """Close ONLY the calling loop's session — never another thread's."""
        import asyncio

        entry = self._sessions.pop(id(asyncio.get_running_loop()), None)
        if entry is not None and not entry[1].closed:
            # aiohttp's close is a coroutine; a test double may be sync —
            # handle both (SearXNG's old close did the same).
            close = entry[1].close
            if asyncio.iscoroutinefunction(close):
                await close()
            else:
                close()
