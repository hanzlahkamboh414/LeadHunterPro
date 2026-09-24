"""Re-derive ``company_match`` for rows stored before it was ever set.

WHY THIS EXISTS
---------------
``company_match`` was never written by any production path (verified by grep
2026-09-21: ``company_match=`` appeared only in tests), so all 224 stored
rows carried the column's DEFAULT — ``unknown`` — and the PAIN_GATE, which
requires ``CONFIRMED`` on every supporting record, blocked all 22 hypotheses
structurally. The adapters now classify it
(:func:`app.research.adapters.classify_company_match`), but that fix alone
cannot repair the rows already on disk, and it cannot even ADD to them:

    ``add_evidence`` is ``INSERT OR IGNORE`` keyed on a deterministic
    ``evidence_id`` and a content hash, so re-running the same research
    produces the same ids, matches the existing rows, and writes nothing.
    The rows would have read ``unknown`` forever.

This is a RECOMPUTATION, not a rewrite of history. The observation — the page
URL, the excerpt, the date — is untouched; only a field that was left at its
default is filled in, from the same deterministic function the live path now
uses. Nothing here can write ``MISMATCH``: the classifier is structurally
incapable of returning it, because proving a record is about a DIFFERENT
company needs that other company's name, which the row does not contain.

Usage (VPS, mirroring the systemd EnvironmentFile):

    sudo bash -c 'set -a; . /opt/leadhunter/.env; set +a; \
      cd /opt/leadhunter/backend && /opt/leadhunter/venv/bin/python \
      scripts/backfill_company_match.py'

Add ``--dry-run`` to print every change and touch nothing — run that first,
always.

The text a row is judged on mirrors the adapters exactly: the excerpt when it
has one, else the title. That is not a shortcut — the AI mapper stores its
claim in ``title`` and leaves ``excerpt`` empty, while the intent and field
mappers do the opposite, so ``excerpt or title`` is what each mapper actually
classified at write time.
"""

from __future__ import annotations

import os
import sqlite3
import sys

# Run as a plain script (python scripts/backfill_company_match.py): put the
# backend root on sys.path so `app.*` resolves (the venv has no install).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.research.adapters import classify_company_match

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "output",
    "research_evidence.db",
)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("DRY RUN — every decision below is printed, nothing is written\n")
    if not os.path.exists(DB_PATH):
        print(f"no research_evidence.db at {DB_PATH} — nothing to backfill")
        return 0

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT e.evidence_id, e.source_type, e.source_url, e.title,
                   e.excerpt, e.company_match,
                   c.name AS company_name, c.company_key AS company_key
            FROM evidence e
            LEFT JOIN companies c ON c.company_id = e.company_id
            """
        ).fetchall()
        print(f"evidence rows: {len(rows)}")

        changes: list[tuple[str, str, str, str]] = []  # id, old, new, why
        unchanged = 0
        no_company = 0
        for row in rows:
            name = row["company_name"] or ""
            key = row["company_key"] or ""
            if not name and not key:
                # A row whose company is gone cannot be judged. Left alone
                # rather than guessed at.
                no_company += 1
                continue
            new = classify_company_match(
                company_name=name,
                domain=key,
                source_url=row["source_url"] or "",
                text=(row["excerpt"] or row["title"] or ""),
            ).value
            old = row["company_match"]
            if new == old:
                unchanged += 1
                continue
            changes.append((
                row["evidence_id"], old, new,
                f"{(row['company_name'] or '')[:26]} | "
                f"{(row['source_type'] or '')[:12]} | "
                f"{(row['excerpt'] or row['title'] or '')[:58]}",
            ))

        tally: dict[tuple[str, str], int] = {}
        for _, old, new, _ in changes:
            tally[(old, new)] = tally.get((old, new), 0) + 1
        print(f"unchanged={unchanged} to-change={len(changes)} "
              f"no-company={no_company}")
        for (old, new), count in sorted(tally.items()):
            print(f"  {old} -> {new}: {count}")

        if not dry_run and changes:
            conn.executemany(
                "UPDATE evidence SET company_match = ? WHERE evidence_id = ?",
                [(new, eid) for eid, _, new, _ in changes],
            )
            conn.commit()
            print(f"\nupdated {len(changes)} row(s)")
        elif changes:
            print(f"\nWOULD update {len(changes)} row(s)")

        # The safety property worth printing, not just asserting: this sweep
        # can only ever move rows TO a state the gate can use, or to an
        # honest "cannot establish". MISMATCH would delete evidence by
        # refusal, and no path here can produce it.
        bad = [c for c in changes if c[2] == "mismatch"]
        if bad:
            print(f"REFUSING: {len(bad)} row(s) would become MISMATCH — "
                  "the classifier must never return it; nothing was written")
            return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
