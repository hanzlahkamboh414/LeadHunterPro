"""One-pass pre-screen of the discovery cache (pending_leads).

Run (from backend/):
    python scripts/sweep_pending.py

Flags every pending lead the research stage would reject anyway — free-mail, or
no MX on the email's domain — as ``dead`` so it is NEVER served and never burns
a research slot on a future run, and drains pending rows that are already
researched dossiers. Runs the SAME cheap gates the pipeline uses (free-mail
triage + fast native MX check) — no AI credits spent. The POST
``/leads/pending/sweep`` endpoint does exactly this against the live store; this
script is the shell-agnostic, inspectable twin.

Honest per-bucket counts, nothing silent (CLAUDE.md §6): total / kept /
free_mail / dead_domain / already_researched.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.lead_research.service import LeadResearchStore, PendingLeadsStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=None,
        help="SQLite db path (default: the live backend/output/lead_research.db)",
    )
    args = parser.parse_args()

    store = LeadResearchStore(db_path=args.db) if args.db else LeadResearchStore()
    pending = PendingLeadsStore(db_path=store._db_path)

    stats = pending.sweep_known_dead(dossier_store=store)
    print("SWEEP:", stats)

    conn = sqlite3.connect(store._db_path)
    kept = conn.execute(
        "SELECT email, location FROM pending_leads WHERE dead = 0 "
        "ORDER BY location, email"
    ).fetchall()
    conn.close()

    by_loc: dict[str, list[str]] = {}
    for email, loc in kept:
        by_loc.setdefault(loc or "(no location)", []).append(email)
    print(f"KEPT total: {len(kept)}")
    for loc, emails in sorted(by_loc.items()):
        print(f"  {loc}: {len(emails)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())