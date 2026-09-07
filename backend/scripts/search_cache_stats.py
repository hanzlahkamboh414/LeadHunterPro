"""Report the persistent search cache — how many paid queries it now replaces.

Read-only by default: no provider is contacted, no credit is spent.

    python scripts/search_cache_stats.py            # summary
    python scripts/search_cache_stats.py --list 20  # newest cached queries
    python scripts/search_cache_stats.py --purge    # drop TTL-expired rows
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.search_providers.cache import SearchCache, default_db_path  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Search-cache report")
    ap.add_argument("--list", type=int, default=0, metavar="N",
                    help="show the N most recently cached queries")
    ap.add_argument("--purge", action="store_true",
                    help="delete TTL-expired rows")
    args = ap.parse_args()

    path = os.path.abspath(default_db_path())
    print("=" * 62)
    print("SEARCH CACHE")
    print("=" * 62)
    print(f"file      : {path}")
    if not os.path.exists(path):
        print("state     : NOT CREATED YET")
        print()
        print("The cache file appears on the first research run. Until then")
        print("every query is a paid provider call.")
        return 0

    size_kb = os.path.getsize(path) / 1024
    cache = SearchCache(path)
    stats = cache.stats()

    print(f"size      : {size_kb:.1f} KB")
    print(f"ttl       : {stats['ttl_days']} days (search) / "
          f"{stats['extract_ttl_days']} days (extract)")
    print(f"queries   : {stats['cached_queries']}")
    print(f"extracts  : {stats['cached_extracts']}")
    print()

    saved = stats["cached_queries"] + stats["cached_extracts"]
    print(f"A re-run over the same leads now costs 0 provider credits for "
          f"{saved} of its calls.")
    print()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    fresh = conn.execute(
        "SELECT MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest FROM search_cache"
    ).fetchone()
    if fresh and fresh["newest"]:
        print(f"oldest    : {fresh['oldest']}")
        print(f"newest    : {fresh['newest']}")
        print()

    if args.list:
        print(f"--- {args.list} most recent cached queries ---")
        rows = conn.execute(
            "SELECT query, max_results, fetched_at, LENGTH(results_json) AS n "
            "FROM search_cache ORDER BY fetched_at DESC LIMIT ?",
            (args.list,),
        ).fetchall()
        for r in rows:
            print(f"  [{r['fetched_at'][:19]}] n={r['max_results']} "
                  f"{r['n']:>6}B  {r['query'][:70]}")
        print()
    conn.close()

    if args.purge:
        removed = cache.purge_expired()
        print(f"purged    : {removed} expired row(s)")
    cache.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
