"""Persistent search-result cache — the credit-burn root-cause fix.

Why this exists
---------------
Every lead costs 15-20 provider calls (5 screening + 4-7 person + 6 deep
queries + 1-2 ``/extract``). Nothing anywhere cached a query, so re-researching
a domain — a top-up round, a re-run of the same trade/location, a second lead
at the SAME company — paid for byte-identical queries again. On a metered
provider that is the entire reason the plan limit keeps running out (Tavily
answers HTTP 432 once the plan's usage cap is hit).

This cache sits at the ``RegistryIndexedSearch`` seam, i.e. ABOVE the registry
and BELOW the research stages, so:

* it is provider-agnostic (CLAUDE.md §3/§4) — swapping Tavily for SearXNG,
  Brave or anything else changes nothing here, and the cache never becomes
  the architecture, only infrastructure in front of it;
* every stage (company screening, deep research, person research, LinkedIn
  extract) benefits from one implementation, no duplication (CLAUDE.md §14).

``app/crawlers/cache.py`` (:class:`ResponseCache`) is deliberately NOT reused:
it is in-memory, so it dies with the process. Credits are burned ACROSS runs
and across backend restarts, so the cache has to be on disk. Same file-backed
SQLite + WAL pattern as the other stores in this repo.

Honesty rules (CLAUDE.md §1/§12) — this cache never invents data:
* Only NON-EMPTY results are stored. At this seam an empty list means either
  "no results" or "the provider errored" (providers never raise; they return
  ``SearchResponse(status="error")`` which flattens to ``[]``). Caching that
  would freeze a transient 432/429 into a permanent "this query has no
  answers" — a silent fake negative. So an empty answer is never cached and
  the query is retried on the next run.
* Entries carry ``fetched_at`` and expire by TTL, so evidence can never be
  cited from indefinitely stale pages.
* A cache read is logged as a HIT with its age; a write as a MISS (CLAUDE.md
  §6 — the log always says where data came from).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.db_paths import operational_db_path

logger = logging.getLogger(__name__)

# Search answers move slowly for the facts this pipeline cites (company
# identity, licences, LinkedIn pages). Two weeks keeps a month-long run cheap
# while still re-checking often enough that "hiring"/"expansion" signals are
# not stale by a whole quarter.
DEFAULT_TTL_DAYS = 14

# Extracted page TEXT is even more stable than a result list, and it is the
# most expensive call (one credit per URL), so it is held longer.
DEFAULT_EXTRACT_TTL_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_cache (
    cache_key TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    max_results INTEGER NOT NULL,
    results_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extract_cache (
    cache_key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    text TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


def default_db_path() -> str:
    """``backend/output/search_cache.db`` — same convention as the other stores.

    A dedicated file, not ``lead_research.db``: the cache is disposable
    infrastructure, so deleting it must never risk a dossier.
    """
    return operational_db_path(os.path.join(
        os.path.dirname(__file__), "..", "..", "output", "search_cache.db"
    ))


def normalize_query(query: str) -> str:
    """Collapse whitespace + casefold so trivially different spellings of the
    SAME query share one cache entry (``' Acme  Corp '`` == ``'acme corp'``).

    Search operators (``site:``, quotes) are preserved verbatim — they change
    the answer, so they must change the key.
    """
    return " ".join((query or "").split()).casefold()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(fetched_at: str) -> float | None:
    """Age of an entry in seconds, or ``None`` when the stamp is unreadable."""
    try:
        stamp = datetime.fromisoformat(fetched_at)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


class SearchCache:
    """SQLite-backed, TTL-expiring cache for search results and page extracts.

    Thread-safe for this local-MVP shape: ``check_same_thread=False`` plus a
    lock, WAL journal for crash-safe concurrent reads — the same contract
    :class:`app.person_research.store.ResearchStore` uses.

    Every method swallows storage errors: a broken cache file must degrade to
    "no cache" (a paid live query) and never break a research run.
    """

    def __init__(
        self,
        path: str | pathlib.Path | None = None,
        *,
        ttl_days: int = DEFAULT_TTL_DAYS,
        extract_ttl_days: int = DEFAULT_EXTRACT_TTL_DAYS,
    ) -> None:
        self.path = str(path or default_db_path())
        self.ttl = timedelta(days=max(0, ttl_days))
        self.extract_ttl = timedelta(days=max(0, extract_ttl_days))
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self.hits = 0
        self.misses = 0
        self.extract_hits = 0
        self.extract_misses = 0
        self._connect()

    # -- lifecycle -----------------------------------------------------------

    def _connect(self) -> None:
        try:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        except Exception as exc:  # noqa: BLE001 — a dead cache must not be fatal
            logger.warning("SEARCH CACHE unavailable (%s): %s", self.path, exc)
            self._conn = None

    @property
    def available(self) -> bool:
        return self._conn is not None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # -- search results ------------------------------------------------------

    @staticmethod
    def _key(query: str, max_results: int) -> str:
        # ``max_results`` is part of the key: a 5-result entry must not be
        # served to a caller that asked for 10, otherwise the pipeline would
        # silently see a truncated web.
        raw = f"{max_results}\n{normalize_query(query)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, query: str, max_results: int) -> list[dict[str, Any]] | None:
        """Cached results for this exact (query, max_results), else ``None``."""
        if self._conn is None or not (query or "").strip():
            return None
        key = self._key(query, max_results)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT results_json, fetched_at FROM search_cache WHERE cache_key=?",
                    (key,),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.debug("SEARCH CACHE read failed: %s", exc)
            return None
        if row is None:
            self.misses += 1
            return None

        age = _age_seconds(row["fetched_at"])
        if age is None or age > self.ttl.total_seconds():
            self.misses += 1
            logger.debug("SEARCH CACHE EXPIRED query=%r age=%ss", query, age)
            return None

        try:
            results = json.loads(row["results_json"])
        except (TypeError, ValueError):
            self.misses += 1
            return None
        if not isinstance(results, list) or not results:
            self.misses += 1
            return None

        self.hits += 1
        logger.info(
            "SEARCH CACHE HIT query=%r results=%d age=%.0fs (0 provider credits)",
            query,
            len(results),
            age,
        )
        return results

    def set(self, query: str, max_results: int, results: list[dict[str, Any]]) -> None:
        """Store NON-EMPTY results. Empty answers are deliberately not cached.

        An empty list at this layer is ambiguous — genuinely no results, or a
        provider error/quota rejection flattened to ``[]``. Persisting it would
        turn a transient failure into a permanent silent negative, which
        CLAUDE.md §1/§12 forbid.
        """
        if self._conn is None or not results or not (query or "").strip():
            return
        key = self._key(query, max_results)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO search_cache "
                    "(cache_key, query, max_results, results_json, fetched_at) "
                    "VALUES (?,?,?,?,?)",
                    (key, query, int(max_results), json.dumps(results), _now()),
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("SEARCH CACHE write failed: %s", exc)

    # -- extracted page text -------------------------------------------------

    @staticmethod
    def _extract_key(url: str, max_length: int) -> str:
        raw = f"{max_length}\n{(url or '').strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_extract(self, url: str, max_length: int) -> str | None:
        if self._conn is None or not (url or "").strip():
            return None
        key = self._extract_key(url, max_length)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT text, fetched_at FROM extract_cache WHERE cache_key=?",
                    (key,),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.debug("EXTRACT CACHE read failed: %s", exc)
            return None
        if row is None:
            self.extract_misses += 1
            return None

        age = _age_seconds(row["fetched_at"])
        if age is None or age > self.extract_ttl.total_seconds():
            self.extract_misses += 1
            return None

        text = row["text"] or ""
        if not text:
            self.extract_misses += 1
            return None

        self.extract_hits += 1
        logger.info(
            "EXTRACT CACHE HIT url=%s chars=%d age=%.0fs (0 provider credits)",
            url,
            len(text),
            age,
        )
        return text

    def set_extract(self, url: str, max_length: int, text: str) -> None:
        """Store non-empty extracted text (empty = unreadable, never cached)."""
        if self._conn is None or not text or not (url or "").strip():
            return
        key = self._extract_key(url, max_length)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO extract_cache "
                    "(cache_key, url, text, fetched_at) VALUES (?,?,?,?)",
                    (key, url, text, _now()),
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("EXTRACT CACHE write failed: %s", exc)

    # -- maintenance / telemetry --------------------------------------------

    def purge_expired(self) -> int:
        """Drop entries past their TTL. Returns the number of rows removed."""
        if self._conn is None:
            return 0
        cutoff = (datetime.now(timezone.utc) - self.ttl).isoformat()
        ex_cutoff = (datetime.now(timezone.utc) - self.extract_ttl).isoformat()
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM search_cache WHERE fetched_at < ?", (cutoff,)
                )
                removed = cur.rowcount or 0
                cur = self._conn.execute(
                    "DELETE FROM extract_cache WHERE fetched_at < ?", (ex_cutoff,)
                )
                removed += cur.rowcount or 0
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("SEARCH CACHE purge failed: %s", exc)
            return 0
        return removed

    def stats(self) -> dict[str, Any]:
        """Counters + row counts, for honest credit-saving telemetry (§6)."""
        rows = extract_rows = 0
        if self._conn is not None:
            try:
                with self._lock:
                    rows = self._conn.execute(
                        "SELECT COUNT(*) AS n FROM search_cache"
                    ).fetchone()["n"]
                    extract_rows = self._conn.execute(
                        "SELECT COUNT(*) AS n FROM extract_cache"
                    ).fetchone()["n"]
            except Exception:  # noqa: BLE001
                pass
        total = self.hits + self.misses
        return {
            "available": self.available,
            "path": self.path,
            "ttl_days": self.ttl.days,
            "extract_ttl_days": self.extract_ttl.days,
            "search_hits": self.hits,
            "search_misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "extract_hits": self.extract_hits,
            "extract_misses": self.extract_misses,
            "cached_queries": rows,
            "cached_extracts": extract_rows,
        }


# ---------------------------------------------------------------------------
# Process-wide singleton — one cache file, one connection, reused by every
# research stage. ``None`` when caching is disabled by config.
# ---------------------------------------------------------------------------
_CACHE: SearchCache | None = None
_CACHE_LOCK = threading.Lock()
_CACHE_INIT = False


def get_search_cache() -> SearchCache | None:
    """Shared :class:`SearchCache`, or ``None`` when disabled in settings."""
    global _CACHE, _CACHE_INIT
    if _CACHE_INIT:
        return _CACHE
    with _CACHE_LOCK:
        if _CACHE_INIT:
            return _CACHE
        try:
            from app.core.config import settings

            enabled = bool(getattr(settings, "SEARCH_CACHE_ENABLED", True))
            ttl = int(getattr(settings, "SEARCH_CACHE_TTL_DAYS", DEFAULT_TTL_DAYS))
            extract_ttl = int(
                getattr(
                    settings, "SEARCH_CACHE_EXTRACT_TTL_DAYS", DEFAULT_EXTRACT_TTL_DAYS
                )
            )
            path = getattr(settings, "SEARCH_CACHE_DB", "") or None
        except Exception:  # noqa: BLE001 — settings unavailable (early import/tests)
            enabled, ttl, extract_ttl, path = True, DEFAULT_TTL_DAYS, DEFAULT_EXTRACT_TTL_DAYS, None
        if enabled:
            _CACHE = SearchCache(
                operational_db_path(path or default_db_path()),
                ttl_days=ttl,
                extract_ttl_days=extract_ttl,
            )
            logger.info(
                "SEARCH CACHE enabled path=%s ttl=%dd extract_ttl=%dd",
                _CACHE.path,
                ttl,
                extract_ttl,
            )
        else:
            _CACHE = None
            logger.info("SEARCH CACHE disabled by config (every query is paid)")
        _CACHE_INIT = True
    return _CACHE


def reset_search_cache() -> None:
    """Drop the singleton (tests / config reload)."""
    global _CACHE, _CACHE_INIT
    with _CACHE_LOCK:
        if _CACHE is not None:
            _CACHE.close()
        _CACHE = None
        _CACHE_INIT = False
