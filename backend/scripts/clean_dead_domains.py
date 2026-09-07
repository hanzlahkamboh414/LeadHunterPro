"""One-time / reusable maintenance: purge dead-domain dossiers (with backup).

Approved cleanup ("dead domain wali emails delete karo"): every stored dossier
rejected at the dead-domain MX gate (rec=skip + 'no MX record' fit) is deleted.
These dossiers MUST never inflate lead totals — a domain with no MX record
cannot receive email, so the address is not a lead.

Safe by construction: the DB is copied to ``output/lead_research.bak-<ts>.db``
BEFORE anything is removed, and the removal count + exact emails are printed
(never silent — CLAUDE.md §6). Run from ``backend/``:

    python scripts/clean_dead_domains.py
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sqlite3
import sys

# Ensure backend is on path (same as run_query.py / research_lead.py) so
# `import app` resolves even though sys.path[0] is this script's own folder
# (scripts/) when invoked by file path.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.lead_research.service import LeadResearchStore


def main() -> int:
    db = os.path.join("output", "lead_research.db")
    if not os.path.exists(db):
        print(f"no DB at {db} — nothing to clean")
        return 0

    db_abs = os.path.abspath(db)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = os.path.join("output", f"lead_research.bak-{ts}.db")
    shutil.copy2(db_abs, bak)
    print(f"backup -> {bak}")

    store = LeadResearchStore(db_path=db)
    before = store.count()

    # List the exact dead-domain dossiers in the BACKUP (pre-clean) so the
    # report is honest even though they are about to be gone from live.
    dead: list[tuple[str, str]] = []
    conn = sqlite3.connect(bak)
    for (j,) in conn.execute("SELECT dossier_json FROM dossiers"):
        d = json.loads(j)
        if d.get("recommendation") == "skip" and "no MX record" in (d.get("fit") or ""):
            dead.append((d.get("email", ""), d.get("domain", "")))
    conn.close()

    removed = store.clean_dead_domain_dossiers()
    after = store.count()

    print(f"removed: {removed}")
    for email, domain in dead:
        print(f"  - {email} | {domain}")
    print(f"dossiers before -> after: {before} -> {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())