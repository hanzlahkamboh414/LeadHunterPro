"""Repo-wide test fixtures.

The persistent search cache (:mod:`app.search_providers.cache`) is a
process-wide singleton pointing at ``backend/output/search_cache.db`` in normal
operation. Tests must never read or write that real file: a cached answer from
a live run would silently satisfy a test that means to exercise the provider
path, and a test's fake results would pollute production credits data.

So every test gets its own empty cache file in a tmp dir, and the singleton is
reset before and after. Tests that specifically assert cache behaviour build
their own :class:`SearchCache` explicitly.
"""

from __future__ import annotations

import pytest

from app.search_providers import cache as search_cache_mod


@pytest.fixture(autouse=True)
def _isolated_search_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        search_cache_mod,
        "default_db_path",
        lambda: str(tmp_path / "search_cache.db"),
    )
    search_cache_mod.reset_search_cache()
    yield
    search_cache_mod.reset_search_cache()
