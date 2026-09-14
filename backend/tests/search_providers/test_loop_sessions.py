"""LoopSessionMixin — one aiohttp session per event loop (concurrency fix).

Live proof 2026-09-11 (Austin proof run): the lead pipeline runs several
CONCURRENT ``asyncio.run`` loops that all share the registry's singleton
provider instances — discovery passes and 3 parallel research threads. With a
single instance-level session, thread B's lazy init overwrote the session
thread A was mid-request on ("Timeout context manager should be used inside a
task") and thread B's ``close()`` killed A's in-flight connector ("Connector
is closed"). These tests pin the per-loop fix: every loop gets its OWN
session, ``close()`` closes only the calling loop's, and dead entries prune.
"""

from __future__ import annotations

import asyncio

from app.search_providers.base import LoopSessionMixin


class _FakeSession:
    """Minimal session double: closed flag + coroutine close()."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Provider(LoopSessionMixin):
    """The mixin host shape: _timeout + _init_sessions()."""

    def __init__(self) -> None:
        self._timeout = 5
        self._init_sessions()


def test_each_loop_gets_its_own_session():
    p = _Provider()

    async def in_loop(tag: str) -> object:
        s = await p._get_session()
        return (tag, s, id(asyncio.get_running_loop()))

    a = asyncio.run(in_loop("a"))
    b = asyncio.run(in_loop("b"))
    # Two different loops -> two DIFFERENT session objects, each keyed by its
    # own loop id (the cross-loop race is impossible by construction). Loop a
    # already CLOSED (asyncio.run tore it down), so its entry was pruned when
    # loop b asked — only b's live entry remains.
    assert a[1] is not b[1]
    assert a[2] != b[2]
    assert len(p._sessions) == 1


def test_two_live_loops_hold_two_sessions():
    p = _Provider()
    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        s_a = loop_a.run_until_complete(p._get_session())
        s_b = loop_b.run_until_complete(p._get_session())
        # Both loops alive at once -> BOTH sessions held, distinct objects.
        assert s_a is not s_b
        assert len(p._sessions) == 2
    finally:
        loop_a.close()
        loop_b.close()


def test_same_loop_reuses_session():
    p = _Provider()

    async def twice() -> tuple[object, object]:
        return await p._get_session(), await p._get_session()

    s1, s2 = asyncio.run(twice())
    assert s1 is s2  # one loop, one session (connection reuse preserved)


def test_close_closes_only_calling_loops_session():
    p = _Provider()
    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        s_a = loop_a.run_until_complete(p._get_session())
        s_b = loop_b.run_until_complete(p._get_session())
        # Loop B closes ITS session (the search_many / _search_then_close
        # pattern: close inside the loop that used it).
        loop_b.run_until_complete(p.close())
        assert s_b.closed
        assert not s_a.closed  # another thread's loop survives B's close()
        assert len(p._sessions) == 1  # only A's entry remains
    finally:
        loop_a.close()
        loop_b.close()


def test_dead_loop_entries_pruned_on_next_use():
    p = _Provider()

    async def make():
        return await p._get_session()

    asyncio.run(make())  # loop #1 — its session dies with its loop
    s2 = asyncio.run(make())  # loop #2
    # Loop #1's entry was pruned when loop #2 asked: only the live one remains.
    assert len(p._sessions) == 1
    assert next(iter(p._sessions.values()))[1] is s2


def test_real_providers_use_the_mixin():
    """All three aiohttp-backed providers must carry the per-loop session fix
    (a provider silently regressing to an instance session reintroduces the
    cross-loop race)."""
    from app.search_providers.brave import BraveSearchProvider
    from app.search_providers.searxng import SearXNGProvider
    from app.search_providers.tavily import TavilySearchProvider

    for cls in (BraveSearchProvider, SearXNGProvider, TavilySearchProvider):
        assert issubclass(cls, LoopSessionMixin), cls.__name__
