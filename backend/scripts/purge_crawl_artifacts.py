"""Purge crawl-artifact emails from the stocked pools (one-shot sweep).

Root cause (fixed in code the same day): the web-crawl regex harvested
addresses straight out of raw page source, and page source carries machine
strings shaped like addresses — Sentry DSNs (``<hex>@sentry.wixpress.com``),
RFC 2606 form placeholders (``you@example.com``), mailing-list archive ids
(``20260828153349.8061-1-odion@...``). The extraction seam and the pending
intake gate now drop them (:func:`app.email.email_cleaner.is_crawl_artifact`);
this script clears the rows that slipped in BEFORE that guard existed.

What it does:
  * ``pending_leads`` — artifact rows are DELETED (they are not leads; a
    slot they occupy is a slot a real contact cannot fill).
  * ``dossiers`` — artifact rows are only REPORTED. A dossier is
    user-visible data with its own admin delete/restore flow; this script
    never touches it. Run the report, then decide case by case.
  * ``phone_leads`` — TDLR rows whose state is not TX are DELETED: the
    old ``_parse_tdlr_row`` split multi-word cities wrongly ("SAN ANTONIO
    TX" -> city=SAN, state=AN), so every such row is a parse-bug victim.
    TDLR is a Texas board, the connector now parses correctly, and the
    phones lane re-stocks the same licenses on its next pass — deletion
    loses nothing.

Usage (VPS, mirroring the systemd EnvironmentFile):

    sudo bash -c 'set -a; . /opt/leadhunter/.env; set +a; \
      cd /opt/leadhunter/backend && /opt/leadhunter/venv/bin/python \
      scripts/purge_crawl_artifacts.py'
"""

from __future__ import annotations

import os
import sqlite3
import sys

# Run as a plain script (python scripts/purge_crawl_artifacts.py): put the
# backend root on sys.path so `app.*` resolves (the venv has no install).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.email.email_cleaner import is_crawl_artifact

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "output",
    "lead_research.db",
)


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"no lead_research.db at {DB_PATH} — nothing to sweep")
        return 0
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        pending = conn.execute(
            "SELECT email, company, source_url, dork FROM pending_leads"
        ).fetchall()
        artifacts = [r for r in pending if is_crawl_artifact(r["email"])]
        print(f"pending_leads: total={len(pending)} artifacts={len(artifacts)}")
        for r in artifacts:
            print(f"  DROP {r['email'][:60]}"
                  f" | {(r['company'] or '')[:30]}"
                  f" | {(r['source_url'] or '')[:60]}")
        if artifacts:
            conn.executemany(
                "DELETE FROM pending_leads WHERE email = ?",
                [(r["email"],) for r in artifacts],
            )
            conn.commit()
            print(f"deleted {len(artifacts)} pending artifact rows")

        dossiers = conn.execute(
            "SELECT email, domain, trade FROM dossiers"
        ).fetchall()
        d_artifacts = [r for r in dossiers if is_crawl_artifact(r["email"])]
        print(f"dossiers: total={len(dossiers)} artifacts={len(d_artifacts)}"
              " (REPORT ONLY — use the admin delete flow, never this sweep)")
        for r in d_artifacts:
            print(f"  REVIEW {r['email'][:60]} | {r['trade'] or ''}")

        phones_path = os.path.join(
            os.path.dirname(os.path.abspath(DB_PATH)), "phone_leads.db",
        )
        if os.path.exists(phones_path):
            phones = sqlite3.connect(phones_path)
            phones.row_factory = sqlite3.Row
            try:
                bad = phones.execute(
                    "SELECT id, trade, state, city, business_name "
                    "FROM phone_leads "
                    "WHERE source_url LIKE '%7358-krk7%' AND state != 'TX'"
                ).fetchall()
                print(f"phone_leads (TDLR): bad-state rows={len(bad)}")
                for r in bad[:15]:
                    print(f"  DROP #{r['id']} {r['trade']}/{r['state']}"
                          f" | {(r['city'] or '')[:12]}"
                          f" | {(r['business_name'] or '')[:34]}")
                if len(bad) > 15:
                    print(f"  ... and {len(bad) - 15} more")
                if bad:
                    phones.execute(
                        "DELETE FROM phone_leads "
                        "WHERE source_url LIKE '%7358-krk7%' AND state != 'TX'"
                    )
                    phones.commit()
                    print(f"deleted {len(bad)} TDLR bad-state rows "
                          "(re-stocked correctly on the next phones pass)")
            finally:
                phones.close()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
