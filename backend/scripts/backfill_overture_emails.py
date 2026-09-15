"""One-off Overture backfill — emails for phone leads already in the pool.

``sync_overture.py`` materialized the phone->email join AFTER the pool was
stocked, so email-less leads sit unenriched even though Overture knows many
of them — and the enrich worker only ever picks CLAIMED leads going forward
(claimed-first policy), so the shared pool would wait forever. This script
walks every email-less phone lead, runs the SAME stage-0 lookup the worker's
``enrich_lead`` would, and stamps hits exactly like a worker pass — including
feeding the emails vertical's pending cache through the same guarded intake
(``PendingLeadsStore.add``: free-mail drop, non-client gate).

Honest by construction: only real Overture hits are written, misses stay
misses, and every count is reported at the end.

Usage (from backend/, after scripts/sync_overture.py):
    python scripts/backfill_overture_emails.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.lead_research.service import PendingLeadsStore  # noqa: E402
from app.phones.overture import OvertureStore  # noqa: E402
from app.phones.store import PhoneLeadsStore  # noqa: E402

#: Phones per lookup batch — the ART-indexed IN-lookup is instant at this
#: width (the worker does one phone per call; a backfill earns batching).
BATCH = 500


def main() -> int:
    phones = PhoneLeadsStore()
    overture = OvertureStore()
    pending = PendingLeadsStore()

    if not overture.is_synced():
        print("overture.duckdb not synced — run scripts/sync_overture.py first")
        return 1
    print(f"overture release: {overture.release()}")

    rows = phones.emailless_leads()
    print(f"email-less phone leads: {len(rows)}")

    hits = fed = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        found = overture.lookup_emails([r["phone"] for r in batch])
        to_feed = []
        for lead in batch:
            hit = found.get(lead["phone"])
            if not hit:
                continue
            hits += 1
            phones.set_enrichment(
                lead["id"],
                email=hit["email"],
                email_source="overture",
                website=hit["website"],
            )
            domain = hit["email"].rsplit("@", 1)[-1]
            to_feed.append({
                "email": hit["email"],
                "domain": domain,
                "company": lead.get("business_name", ""),
                "person": lead.get("person_name", ""),
                "source_url": hit["website"] or lead.get("source_url", ""),
                "location": ", ".join(
                    x for x in (lead.get("city", ""), lead.get("state", ""))
                    if x),
                "dork": "overture",
                "trade": lead.get("trade", ""),
            })
        if to_feed:
            fed += pending.add(to_feed)
        print(f"  {min(start + BATCH, len(rows))}/{len(rows)} scanned, "
              f"{hits} overture hits, {fed} stocked to emails vertical")

    print(f"done: {hits}/{len(rows)} phone leads gained an Overture email; "
          f"{fed} accepted into the emails-vertical pending cache "
          f"(the rest were free-mail/non-client — honestly dropped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
