"""Quarantine old website-enriched phone emails with no company/host match.

The old enrichment path trusted the first non-directory search hit. This
one-time cleanup preserves original rows in audit tables. Dry-run by default.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

GENERIC = {
    "construction", "contracting", "contractors", "concrete", "electric",
    "electrical", "roofing", "plumbing", "painting", "flooring", "services",
    "service", "group", "company", "incorporated", "limited", "corporation",
    "landscape", "landscaping", "drywall", "mechanical", "industrial",
    "commercial", "building", "builders", "general", "design", "systems",
    "solutions", "enterprises", "associates", "home", "homes", "and", "the",
}


def suspect(business: str, email: str, website: str) -> bool:
    host = (urlparse(website or "").hostname or "").lower().removeprefix("www.")
    if not host or "@" not in (email or ""):
        return False
    tokens = [t for t in re.findall(r"[a-z0-9]+", (business or "").lower())
              if len(t) >= 4 and t not in GENERIC]
    return bool(tokens) and not any(t in host for t in tokens)


def run(phone_db: Path, research_db: Path, apply: bool) -> tuple[int, int]:
    phone = sqlite3.connect(phone_db)
    phone.row_factory = sqlite3.Row
    research = sqlite3.connect(research_db)
    research.row_factory = sqlite3.Row
    try:
        rows = phone.execute(
            "SELECT id, business_name, email, email_source, website "
            "FROM phone_leads WHERE email_source='website' AND email<>''"
        ).fetchall()
        flagged = [r for r in rows if suspect(r["business_name"], r["email"], r["website"])]
        pending = 0
        for r in flagged:
            pending += research.execute(
                "SELECT count(*) FROM pending_leads WHERE lower(email)=lower(?) "
                "AND lower(company)=lower(?) AND dead=0 AND attempt_count=0 AND gated=0",
                (r["email"], r["business_name"]),
            ).fetchone()[0]
        if not apply:
            return len(flagged), pending

        phone.execute("""CREATE TABLE IF NOT EXISTS quarantined_phone_emails (
            phone_lead_id INTEGER PRIMARY KEY, business_name TEXT, email TEXT,
            email_source TEXT, website TEXT, quarantined_at TEXT NOT NULL
        )""")
        research.execute("""CREATE TABLE IF NOT EXISTS quarantined_pending_phone_emails AS
            SELECT *, '' AS quarantined_at FROM pending_leads WHERE 0""")
        for r in flagged:
            phone.execute(
                "INSERT OR IGNORE INTO quarantined_phone_emails "
                "(phone_lead_id,business_name,email,email_source,website,quarantined_at) "
                "VALUES (?,?,?,?,?,datetime('now'))",
                (r["id"], r["business_name"], r["email"], r["email_source"], r["website"]),
            )
            phone.execute(
                "UPDATE phone_leads SET email='', email_source='', website='', "
                "enriched_at='', updated_at=datetime('now') "
                "WHERE id=? AND email=? AND website=? AND email_source='website'",
                (r["id"], r["email"], r["website"]),
            )
            where = ("lower(email)=lower(?) AND lower(company)=lower(?) "
                     "AND dead=0 AND attempt_count=0 AND gated=0")
            args = (r["email"], r["business_name"])
            research.execute(
                "INSERT INTO quarantined_pending_phone_emails "
                "SELECT *, datetime('now') FROM pending_leads WHERE " + where, args,
            )
            research.execute("DELETE FROM pending_leads WHERE " + where, args)
        phone.commit()
        research.commit()
        return len(flagged), pending
    finally:
        phone.close()
        research.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone-db", type=Path, required=True)
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    n, pending = run(args.phone_db, args.research_db, args.apply)
    print(f"{'Quarantined' if args.apply else 'Would quarantine'} {n} phone emails "
          f"and {pending} unattempted pending research rows")
