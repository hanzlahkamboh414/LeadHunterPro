"""One-time (monthly) Overture sync — stock output/overture.duckdb.

Runs the REAL bulk join live: latest release discovery over anonymous S3,
then a single materializing pass into the slim place_contacts table
(phone, email, website). Takes ~10 minutes and a few GB of disk the first
time; afterwards lookups are local and instant (the enrich worker's
stage 0). Re-running replaces the table wholesale with the new release.

Usage (from backend/):
    python scripts/sync_overture.py            # latest release
    python scripts/sync_overture.py 2026-08-19.0   # pin a release
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.phones.overture import OvertureStore  # noqa: E402 — path set above


def main() -> int:
    release = sys.argv[1] if len(sys.argv) > 1 else ""
    store = OvertureStore()
    if store.is_synced():
        print(f"current local release: {store.release()}")
    print(f"syncing {'release ' + release if release else 'latest release'}...")
    result = store.sync(release=release)
    print(f"done: {result['contacts']} phone->email contacts "
          f"from release {result['release']}")
    print(f"local file: {store._db_path}")  # noqa: SLF001 — CLI report
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
