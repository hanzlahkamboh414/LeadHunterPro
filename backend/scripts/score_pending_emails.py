"""P5-Lite backlog scorer — confidence report for the pending-leads pool.

Reads the live ``pending_leads`` table (emails waiting to be researched),
runs the heuristic verifier over every live row (one authoritative MX
check per unique domain), and prints an honest breakdown:

  * per-confidence counts (high / medium / low / unknown / dead)
  * per-reason counts (WHY each verdict was reached)
  * per-provider counts (which mail infrastructure serves the pool)

This is a READ-ONLY report — it never writes to the database. The DOA
purge itself happens inside the pipeline when a run consumes the cache.

Usage (from backend/):
    python scripts/score_pending_emails.py [--db PATH] [--limit N] [--json]

``--db`` defaults to the DATABASE_URL sqlite path from the environment
(or backend/output/lead_research.db).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.email.heuristic_verifier import (  # noqa: E402
    classify_emails,
    get_email_classifier,
)


def _default_db() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "output", "lead_research.db",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=_default_db(),
                    help="lead_research.db path (read-only)")
    ap.add_argument("--limit", type=int, default=0,
                    help="score at most N live leads (0 = all)")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    sql = ("SELECT email FROM pending_leads "
           "WHERE dead = 0 AND gated = 0 AND email != ''")
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    emails = [r[0] for r in conn.execute(sql)]
    conn.close()

    classifier = get_email_classifier()
    outcome_lookup = classifier  # already consults the bounce store
    verdicts = classify_emails(emails)

    by_confidence = collections.Counter(v["confidence"] for v in verdicts)
    by_reason = collections.Counter(
        r for v in verdicts for r in v["reasons"])
    by_provider = collections.Counter(
        v["provider"] for v in verdicts if v["provider"])

    if args.json:
        print(json.dumps({
            "scored": len(verdicts),
            "confidence": dict(by_confidence),
            "reasons": dict(by_reason),
            "providers": dict(by_provider),
        }, indent=2))
        return 0

    print(f"pending-leads backlog scored: {len(verdicts)} live email(s)")
    print(f"db: {args.db}")
    print("\n== confidence ==")
    for tier in ("high", "medium", "low", "unknown", "dead"):
        if by_confidence.get(tier):
            print(f"  {tier:<8} {by_confidence[tier]}")
    print("\n== reasons ==")
    for reason, n in by_reason.most_common():
        print(f"  {reason:<20} {n}")
    print("\n== providers (MX infrastructure) ==")
    for provider, n in by_provider.most_common():
        print(f"  {provider:<12} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
