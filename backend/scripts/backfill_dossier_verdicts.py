"""Re-derive the STORED verdict columns for dossiers researched under old rules.

WHY THIS EXISTS
---------------
``dossiers.potential_score`` and ``dossiers.recommendation`` are written ONCE,
at research time. Read time re-applies today's rules on top
(:func:`app.lead_research.scoring.regate_verdict`) so the leads page never
SHOWS a stale number — but three things read the stored columns DIRECTLY and
cannot be healed by looking at a page:

  1. ``LeadResearchStore.query_leads(min_score=...)`` — filters in SQL on
     ``d.potential_score``.
  2. ``LeadResearchStore.serve_shared`` — pre-filters candidates on
     ``d.potential_score >= 3.0``.
  3. ``all_matching(min_score=...)`` — the CSV export subset.

So a dossier whose real score is 2.5 but which the old formula pushed to 3.0
(the 2026-09-21 timing fix removed a +0.5 that ``intent_timing`` granted when
the AI call FAILED — measured: 1058 of 1179 dossiers carried that sentinel)
stayed eligible for the shared pool and stayed inside a ``min_score`` subset.

This is a RECOMPUTATION, not a rewrite of history. The research itself — the
company, the person, the evidence, the citations — is untouched. Only the two
derived columns are re-derived, from the SAME function the read path uses
(:func:`app.lead_research.scoring.regate_verdict`), so after this sweep the SQL
filters and the page agree.

397 of the 1179 stored rows are NOT touched, and that is deliberate. They were
abandoned by a pre-scoring gate (junk domain, free mail, dead domain, client
fit) and carry ``potential_score = 0.0`` because Stage 4 never ran — the 0.0 is
a leftover default, not a measurement. Re-deriving a score for them computed a
number from fields that were never researched and manufactured a PASSING score
for 43 companies the pipeline had explicitly decided are not our clients.
:func:`~app.lead_research.scoring.has_measured_score` tells the two apart, so
this script can only ever correct a real measurement — never invent one.

It is also NOT the only defence: read time re-derives regardless, so a future
rule change is VISIBLE without a new script. The script exists so the columns
the SQL filters run on are true again.

Usage:

    python scripts/backfill_dossier_verdicts.py --dry-run
    python scripts/backfill_dossier_verdicts.py
    python scripts/backfill_dossier_verdicts.py --dry-run --db /path/to/lead_research.db

``--db`` exists so the SAME script can be rehearsed against a copy of another
instance's database (e.g. a downloaded server snapshot) before it is ever run
where it matters. It defaults to this checkout's ``output/lead_research.db``.

Run ``--dry-run`` first, always.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass

# Run as a plain script (python scripts/backfill_dossier_verdicts.py): put the
# backend root on sys.path so `app.*` resolves (the venv has no install).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.lead_research.models import LeadDossier
from app.lead_research.scoring import has_measured_score, regate_verdict

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "output",
    "lead_research.db",
)

#: The floor ``serve_shared`` and the ``min_score`` filters use. Named here
#: only so the report can point at the membership change it causes.
_FLOOR = 3.0


@dataclass(frozen=True)
class Change:
    """One row whose stored verdict differs from today's rules."""

    email_hash: str
    email: str
    old_score: float
    new_score: float
    old_rec: str
    new_rec: str

    @property
    def fell_below_floor(self) -> bool:
        return self.old_score >= _FLOOR > self.new_score


def _resolve_db_path(argv: list[str]) -> str:
    """Return the database to read — ``--db PATH`` if given, else this checkout's.

    Deliberately narrow: the flag takes the NEXT argument only, so an empty
    ``--db`` falls back to the default rather than silently pointing at "".
    """
    if "--db" in argv:
        index = argv.index("--db")
        if index + 1 < len(argv) and argv[index + 1].strip():
            return argv[index + 1]
    return DEFAULT_DB_PATH


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    db_path = _resolve_db_path(sys.argv)
    if dry_run:
        print("DRY RUN — every decision below is printed, nothing is written\n")
    print(f"database: {db_path}")
    if not os.path.exists(db_path):
        print(f"no lead_research.db at {db_path} — nothing to backfill")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT email_hash, email, dossier_json, recommendation, "
            "potential_score FROM dossiers"
        ).fetchall()
        print(f"dossier rows: {len(rows)}")

        changes: list[Change] = []
        unchanged = 0
        unreadable = 0
        abandoned = 0
        for row in rows:
            try:
                d = LeadDossier.from_dict(json.loads(row["dossier_json"] or "{}"))
            except Exception as exc:  # noqa: BLE001 — report, never guess
                unreadable += 1
                print(f"  SKIP {row['email']}: unreadable dossier_json ({exc})")
                continue
            if not has_measured_score(d):
                # No measurement to correct — see the module docstring.
                abandoned += 1
                continue
            new_rec, new_score = regate_verdict(d)
            old_rec = row["recommendation"] or "skip"
            old_score = float(row["potential_score"] or 0.0)
            if new_rec == old_rec and new_score == old_score:
                unchanged += 1
                continue
            changes.append(Change(
                email_hash=row["email_hash"], email=row["email"] or "",
                old_score=old_score, new_score=new_score,
                old_rec=old_rec, new_rec=new_rec,
            ))

        print(f"unchanged={unchanged} to-change={len(changes)} "
              f"abandoned={abandoned} unreadable={unreadable}")

        rec_tally: dict[str, int] = {}
        for c in changes:
            label = f"{c.old_rec} -> {c.new_rec}"
            rec_tally[label] = rec_tally.get(label, 0) + 1
        for label, count in sorted(rec_tally.items()):
            print(f"  {label}: {count}")
        print(f"  score changed on {sum(1 for c in changes if c.old_score != c.new_score)} row(s)")

        # The membership change this script exists to make: rows the SQL
        # pre-filters newly EXCLUDE. Printed by name, not just counted.
        fell = [c for c in changes if c.fell_below_floor]
        if fell:
            print(f"\nnewly below the {_FLOOR} serve/export floor: {len(fell)}")
            for c in fell[:20]:
                print(f"  {c.email}  {c.old_score:.1f} -> {c.new_score:.1f}  "
                      f"({c.old_rec} -> {c.new_rec})")
            if len(fell) > 20:
                print(f"  ... and {len(fell) - 20} more")

        if not dry_run and changes:
            conn.executemany(
                "UPDATE dossiers SET recommendation = ?, potential_score = ? "
                "WHERE email_hash = ?",
                [(c.new_rec, c.new_score, c.email_hash) for c in changes],
            )
            conn.commit()
            print(f"\nupdated {len(changes)} row(s)")
        elif changes:
            print(f"\nWOULD update {len(changes)} row(s)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
