"""
One-time backfill: fix stale job counts in lead_research.db.

Honesty rule (§6 / CLAUDE.md): leads_found = len(job.results),
working_leads = count of results where recommendation == "contact_now".
The old code computed counts from pass_log (inflated) or set them to 0
on interrupt (recover_orphans before P1 fix existed). This script reads
the truth from job.results and overwrites the stale columns.

Usage:  python scripts/backfill_honest_counts.py
"""
import sys
import os
import json

# --- resolve paths ---
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app.leads.models import Job, JobState
from app.leads.jobs import JobStore


def honest_counts(results: list[dict]) -> tuple[int, int]:
    """leads_found = total results, working = contact_now recommendation."""
    leads_found = len(results)
    working = sum(
        1 for r in results
        if isinstance(r, dict) and (r.get("recommendation") or "") == "contact_now"
    )
    return leads_found, working


def main():
    db_path = os.path.join(BACKEND, "output", "lead_research.db")
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}")
        sys.exit(1)

    store = JobStore(db_path=db_path)
    all_jobs = store.list_all()
    print(f"Total jobs in DB: {len(all_jobs)}")
    print()

    updates = []
    for job in all_jobs:
        results = job.results or []
        if not results:
            continue
        real_found, real_working = honest_counts(results)
        old_found = job.leads_found or 0
        old_working = job.working_leads or 0
        if real_found != old_found or real_working != old_working:
            updates.append((job, old_found, old_working, real_found, real_working))

    if not updates:
        print("All job counts are honest. Nothing to update.")
        return

    print(f"Dishonest rows found: {len(updates)}")
    print()
    print(f"{'Job ID':<16} {'State':<12} {'Old (f/w)':<14} {'New (f/w)':<14}")
    print("-" * 60)
    for job, of, ow, nf, nw in updates:
        print(f"{job.id:<16} {job.state.value:<12} {of}/{ow:<12} {nf}/{nw}")

    print()
    confirm = input("Proceed with backfill? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        sys.exit(0)

    updated = 0
    for job, of, ow, nf, nw in updates:
        job.leads_found = nf
        job.working_leads = nw
        store.save(job)
        updated += 1
        print(f"  Updated {job.id}: {of}/{ow} -> {nf}/{nw}")

    print(f"\nDone. {updated} rows updated.")

    # Verify
    print("\n--- Verification ---")
    for job, _of, _ow, _nf, _nw in updates:
        fresh = store.get(job.id)
        v_found, v_working = honest_counts(fresh.results or [])
        ok = fresh.leads_found == v_found and fresh.working_leads == v_working
        status = "OK" if ok else "MISMATCH"
        print(f"  {job.id}: stored={fresh.leads_found}/{fresh.working_leads} "
              f"verify={v_found}/{v_working} [{status}]")


if __name__ == "__main__":
    main()
