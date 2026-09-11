"""One-time backfill — seed identity-rejection learning from past user deletions.

Phase E: when a dossier is deleted with an ``irrelevant`` / ``not our client``
reason, its company+domain now feed fit-learning so it stays purged on future
discovery passes. Live DBs that accumulated such deletions BEFORE Phase E (the
2026-09-08 purge) carry them only in ``deleted_leads`` — this script replays
that audit trail into the learning table so the first re-run after upgrade
behaves exactly like a fresh Phase E deletion.

Run (from backend/):
    python scripts/backfill_fit_rejections.py [db_path]

IDEMPOTENT — ``reject()`` accumulates a count per domain, so re-running only
increments the audit counter and never drops a later user rejection. Read-only
on ``dossiers``; writes only to the ``fit_learning`` table.
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.lead_research.fit_learning import FitLearningStore  # noqa: E402
from app.lead_research.service import _is_rejection_reason  # noqa: E402


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    store = FitLearningStore(db_path)
    # The same file LeadResearchStore writes deleted_leads to, so the replay
    # reads the AUDIT the deletions themselves produced.
    from app.lead_research.service import LeadResearchStore

    leads_store = LeadResearchStore(db_path)

    conn = sqlite3.connect(leads_store._db_path)
    rows = conn.execute(
        "SELECT email, reason FROM deleted_leads ORDER BY deleted_at"
    ).fetchall()
    conn.close()

    seeded: list[str] = []
    for email, reason in rows:
        if not _is_rejection_reason(reason):
            continue  # "junk" / "manual" deletes carry no rejection verdict
        if "@" not in (email or ""):
            continue
        domain = (email or "").rsplit("@", 1)[1].strip().lower()
        if not domain:
            continue
        store.reject_domain(domain)
        if domain not in seeded:
            seeded.append(domain)

    print(f"deleted_leads rows: {len(rows)}")
    print(f"rejection-class deletions: {len(seeded)} distinct domains seeded")
    for d in sorted(seeded):
        print(f"  rejected: {d}")
    print("re-run safe — reject() only increments an audit counter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())