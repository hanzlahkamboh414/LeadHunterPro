"""Repo-wide test fixtures.

The persistent search cache (:mod:`app.search_providers.cache`) is a
process-wide singleton pointing at ``backend/output/search_cache.db`` in normal
operation. Tests must never read or write that real file: a cached answer from
a live run would silently satisfy a test that means to exercise the provider
path, and a test's fake results would pollute production credits data.

So every test gets its own empty cache file in a tmp dir, and the singleton is
reset before and after. Tests that specifically assert cache behaviour build
their own :class:`SearchCache` explicitly.

The same hermeticity applies to the source scout (P9): the phones lane serves
from ``soda.effective_trade_coverage()``, which reads whatever is PROMOTED in
``backend/output/source_scout.db``. Tests must never see that real file — a
dev machine that ran ``scripts/scout_pass.py`` would leak its promoted
sources into every coverage assertion. So every test gets a scout store of
None (no coverage, no serving); tests that exercise scout wiring monkeypatch
``soda._scout_store`` themselves.
"""

from __future__ import annotations

import pytest

from app.phones import soda as soda_mod
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


@pytest.fixture(autouse=True)
def _no_scout_sources(monkeypatch):
    """No promoted scout sources leak into any test (see module docstring)."""
    monkeypatch.setattr(soda_mod, "_scout_store", lambda: None)
