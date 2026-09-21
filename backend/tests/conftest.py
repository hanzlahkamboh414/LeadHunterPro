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

And to the research evidence store (Company Signal Intelligence Engine,
Phase 0): ``app.research.store.default_db_path()`` points at
``backend/output/research_evidence.db``. A test that constructed
``ResearchEvidenceStore()`` with no path would create or read that real file,
so the default is redirected into the tmp dir. Tests exercising the store
build it with an explicit ``tmp_path`` path (the ``_store`` helper in each
test module), which is the primary defence; this fixture is the backstop for
any future call site that forgets.

Phase 1 adds one more hermeticity requirement: the intent-evidence intake
(``app.research.intake``) drives three LIVE network plugins from inside
``AILeadResearchAgent.research``. A test that merely builds a real agent
would therefore make real HTTP requests, and its result would depend on
whether USAspending happened to be up. So the feature flag is forced OFF for
every test; the tests that actually exercise the hook turn it back on AND
inject stub plugins (``intent_evidence_plugins``), which is the seam that
exists for exactly this.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.phones import soda as soda_mod
from app.research import store as research_store_mod
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


@pytest.fixture(autouse=True)
def _isolated_research_evidence(tmp_path, monkeypatch):
    """The real ``research_evidence.db`` is never touched (see docstring)."""
    monkeypatch.setattr(
        research_store_mod,
        "default_db_path",
        lambda: str(tmp_path / "research_evidence.db"),
    )


@pytest.fixture(autouse=True)
def _no_live_intent_evidence(monkeypatch):
    """No test hits the three live intent endpoints (see docstring).

    The intake is opt-in per test: turn the flag back on with
    ``monkeypatch.setattr(settings, "INTENT_EVIDENCE_ENABLED", True)`` and
    pass stub plugins via ``intent_evidence_plugins=[...]``.
    """
    monkeypatch.setattr(settings, "INTENT_EVIDENCE_ENABLED", False)
