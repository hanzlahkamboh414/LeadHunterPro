"""Persistent search cache — the credit-burn fix.

The cache must be: durable across processes (credits burn across restarts),
TTL-expiring (no indefinitely stale evidence), and HONEST — an empty answer is
never cached, because at this layer "empty" may be a provider error or quota
rejection and freezing that in would be a silent fake negative
(CLAUDE.md §1/§12).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.search_providers.cache import (
    SearchCache,
    get_search_cache,
    normalize_query,
    reset_search_cache,
)

_RESULTS = [
    {"url": "https://acme.example/about", "snippet": "Acme Construction", "title": "About"},
]


def _cache(tmp_path, **kw) -> SearchCache:
    return SearchCache(tmp_path / "sc.db", **kw)


# -- basics -----------------------------------------------------------------


def test_miss_then_hit(tmp_path):
    c = _cache(tmp_path)
    assert c.get("acme construction", 5) is None
    c.set("acme construction", 5, _RESULTS)
    assert c.get("acme construction", 5) == _RESULTS
    assert c.hits == 1


def test_survives_a_new_process(tmp_path):
    """Durability is the whole point: an in-memory cache cannot fix a bill
    that accrues across backend restarts."""
    first = _cache(tmp_path)
    first.set("q1", 5, _RESULTS)
    first.close()

    second = _cache(tmp_path)
    assert second.get("q1", 5) == _RESULTS


def test_max_results_is_part_of_the_key(tmp_path):
    """A 5-result entry must not answer a 10-result request — otherwise the
    pipeline silently sees a truncated web."""
    c = _cache(tmp_path)
    c.set("q", 5, _RESULTS)
    assert c.get("q", 5) == _RESULTS
    assert c.get("q", 10) is None


def test_query_normalization_shares_one_entry(tmp_path):
    c = _cache(tmp_path)
    c.set("  Acme   Corp ", 5, _RESULTS)
    assert c.get("acme corp", 5) == _RESULTS


def test_search_operators_change_the_key(tmp_path):
    """`site:`/quotes change the answer, so they must change the key."""
    c = _cache(tmp_path)
    c.set('"acme.com"', 5, _RESULTS)
    assert c.get('"acme.com" site:linkedin.com/company', 5) is None
    assert normalize_query('"acme.com"') != normalize_query('"acme.com" site:x.com')


# -- honesty ----------------------------------------------------------------


def test_empty_results_are_never_cached(tmp_path):
    """An empty answer may be a provider error / quota rejection (providers
    never raise — they flatten to []). Caching it would turn a transient
    failure into a permanent silent negative."""
    c = _cache(tmp_path)
    c.set("q", 5, [])
    assert c.get("q", 5) is None
    assert c.stats()["cached_queries"] == 0


def test_blank_query_is_ignored(tmp_path):
    c = _cache(tmp_path)
    c.set("   ", 5, _RESULTS)
    assert c.get("   ", 5) is None


def test_expired_entry_is_a_miss(tmp_path):
    c = _cache(tmp_path, ttl_days=14)
    c.set("q", 5, _RESULTS)
    stale = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    conn = sqlite3.connect(str(tmp_path / "sc.db"))
    conn.execute("UPDATE search_cache SET fetched_at=?", (stale,))
    conn.commit()
    conn.close()
    assert c.get("q", 5) is None


def test_unreadable_timestamp_is_a_miss(tmp_path):
    c = _cache(tmp_path)
    c.set("q", 5, _RESULTS)
    conn = sqlite3.connect(str(tmp_path / "sc.db"))
    conn.execute("UPDATE search_cache SET fetched_at=?", ("not-a-date",))
    conn.commit()
    conn.close()
    assert c.get("q", 5) is None


def test_corrupt_payload_is_a_miss(tmp_path):
    c = _cache(tmp_path)
    c.set("q", 5, _RESULTS)
    conn = sqlite3.connect(str(tmp_path / "sc.db"))
    conn.execute("UPDATE search_cache SET results_json=?", ("{not json",))
    conn.commit()
    conn.close()
    assert c.get("q", 5) is None


# -- extract ----------------------------------------------------------------


def test_extract_roundtrip_and_ttl(tmp_path):
    c = _cache(tmp_path, extract_ttl_days=30)
    assert c.get_extract("https://x.example/p", 4000) is None
    c.set_extract("https://x.example/p", 4000, "page text")
    assert c.get_extract("https://x.example/p", 4000) == "page text"
    # a different max_length is a different entry (different truncation)
    assert c.get_extract("https://x.example/p", 1000) is None


def test_empty_extract_is_never_cached(tmp_path):
    """Empty text means "unreadable page" — a later run must retry it."""
    c = _cache(tmp_path)
    c.set_extract("https://x.example/p", 4000, "")
    assert c.get_extract("https://x.example/p", 4000) is None


def test_expired_extract_is_a_miss(tmp_path):
    c = _cache(tmp_path, extract_ttl_days=30)
    c.set_extract("https://x.example/p", 4000, "text")
    stale = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    conn = sqlite3.connect(str(tmp_path / "sc.db"))
    conn.execute("UPDATE extract_cache SET fetched_at=?", (stale,))
    conn.commit()
    conn.close()
    assert c.get_extract("https://x.example/p", 4000) is None


# -- maintenance / telemetry ------------------------------------------------


def test_purge_expired_removes_only_stale_rows(tmp_path):
    c = _cache(tmp_path, ttl_days=14, extract_ttl_days=30)
    c.set("fresh", 5, _RESULTS)
    c.set("stale", 5, _RESULTS)
    c.set_extract("https://x.example/fresh", 4000, "t")
    stale = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    conn = sqlite3.connect(str(tmp_path / "sc.db"))
    conn.execute("UPDATE search_cache SET fetched_at=? WHERE query=?", (stale, "stale"))
    conn.commit()
    conn.close()

    assert c.purge_expired() == 1
    assert c.get("fresh", 5) == _RESULTS
    assert c.get("stale", 5) is None
    assert c.get_extract("https://x.example/fresh", 4000) == "t"


def test_stats_reports_honest_counters(tmp_path):
    c = _cache(tmp_path)
    c.get("miss", 5)
    c.set("hit", 5, _RESULTS)
    c.get("hit", 5)
    s = c.stats()
    assert s["available"] is True
    assert s["search_hits"] == 1
    assert s["search_misses"] == 1
    assert s["hit_rate"] == 0.5
    assert s["cached_queries"] == 1


def test_unwritable_path_degrades_to_no_cache(tmp_path):
    """A broken cache file must mean "every query is paid", never a crash."""
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    c = SearchCache(blocker / "nested" / "sc.db")
    assert c.available is False
    assert c.get("q", 5) is None
    c.set("q", 5, _RESULTS)  # must not raise
    assert c.stats()["cached_queries"] == 0


# -- singleton --------------------------------------------------------------


def test_singleton_is_shared_and_resettable(tmp_path, monkeypatch):
    from app.search_providers import cache as mod

    monkeypatch.setattr(mod, "default_db_path", lambda: str(tmp_path / "single.db"))
    reset_search_cache()
    a = get_search_cache()
    b = get_search_cache()
    assert a is b and a is not None
    reset_search_cache()
    assert get_search_cache() is not a


def test_singleton_disabled_by_settings(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEARCH_CACHE_ENABLED", False)
    reset_search_cache()
    assert get_search_cache() is None
    reset_search_cache()


def test_stored_payload_is_plain_json(tmp_path):
    """The cache file must stay inspectable — a credit audit reads it directly."""
    c = _cache(tmp_path)
    c.set("q", 5, _RESULTS)
    conn = sqlite3.connect(str(tmp_path / "sc.db"))
    row = conn.execute("SELECT query, max_results, results_json FROM search_cache").fetchone()
    conn.close()
    assert row[0] == "q"
    assert row[1] == 5
    assert json.loads(row[2]) == _RESULTS
