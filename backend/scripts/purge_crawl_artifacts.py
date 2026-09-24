"""Purge non-contact emails from the stocked pools (one-shot sweep).

Root cause (fixed in code the same day): the web-crawl regex harvested
addresses straight out of raw page source, and page source carries machine
strings shaped like addresses — Sentry DSNs (``<hex>@sentry.wixpress.com``),
RFC 2606 form placeholders (``you@example.com``), mailing-list archive ids
(``20260828153349.8061-1-odion@...``). The extraction seam, the pending
intake gate and every parser now run the ONE gate
(:func:`app.email.email_cleaner.is_acceptable_email`); this script clears the
rows that slipped in before it existed.

The gate this script applies was WIDENED on 2026-09-21, and the widening is
what the live data asked for. Measured against the local ``lead_research.db``
the same day: the OLD artifact gate matched 4 dossier rows and the new gate
matches 10 — and all 6 extra rows are IMAGE FILENAMES the crawl regex read as
addresses (``logo@3x-1-236x60.png``, ``badge_25_2@2x.png``,
``gaven-logo@2x.png``, ``cropped-favicon@1x-1-180x180.png``, ...). Those were
filtered on the phones path alone, whose copy of the rules carried the image
list; the research path's copy did not. That is the triplicated-validator
defect in one table.

What it does:
  * ``pending_leads`` — non-contact rows are DELETED (they are not leads; a
    slot they occupy is a slot a real contact cannot fill).
  * ``dossiers`` — non-contact rows are only REPORTED. A dossier is
    user-visible data with its own admin delete/restore flow; this script
    never touches it. Run the report, then decide case by case.
  * ``phone_leads`` — TDLR rows whose ``state`` is neither EMPTY nor a
    two-letter USPS code are DELETED. Those are parse FRAGMENTS: the old
    ``_parse_tdlr_row`` split multi-word cities wrongly, so "SAN ANTONIO TX"
    stored ``state="AN"`` ("CH" out of CORPUS CHRISTI, "EL" out of EL PASO —
    observed live 2026-09-15 across 40+ rows). Such a row carries a
    corrupted CITY as well as a corrupted state and is not usable.

    An EMPTY state is left alone, the same way ``_is_junk`` leaves an empty
    email alone: blank is "not known yet", not "known to be junk", and
    deleting a row for missing data is the over-reach this branch already
    committed once.

    The gate is MEMBERSHIP, never ``state != 'TX'``. The current parser
    walks ``business_city_state_zip`` and accepts a state token only when it
    is a real USPS code, so ``state`` is either ``"TX"`` or a genuine
    out-of-state code. TDLR licenses contractors whose mailing address is
    elsewhere, and such a company still holds a Texas licence and can work in
    Texas — a legitimate lead. An equality test cannot tell that row apart
    from a fragment and deletes it (it did: 31 valid rows on 2026-09-22,
    restored from snapshot — see ``restore_tdlr_rows.py``). Against post-fix
    data the fragment set is empty BY CONSTRUCTION: this branch is a clean
    no-op, not a standing deletion.

Usage (VPS, mirroring the systemd EnvironmentFile):

    sudo bash -c 'set -a; . /opt/leadhunter/.env; set +a; \
      cd /opt/leadhunter/backend && /opt/leadhunter/venv/bin/python \
      scripts/purge_crawl_artifacts.py'

Add ``--dry-run`` to print every decision and touch nothing — run that
first, always. On a destructive sweep the report IS the artefact; the
delete is the afterthought.

``--purge-phone-emails`` additionally clears a non-contact email off a
``phone_leads`` row and clears its ``enriched_at``, so the row re-queues
for enrichment instead of sitting stamped as 'honest empty'. Off by
default: re-enrichment spends search credits and SMTP probes. This is the
repair for the user-reported "phone number pr jo emails hain wo theek nahi
hain" — the address usually EXISTS on the page and we mis-read it (a phone
welded onto it by unseparated text extraction, fixed 2026-09-21).
"""

from __future__ import annotations

import os
import sqlite3
import sys

# Run as a plain script (python scripts/purge_crawl_artifacts.py): put the
# backend root on sys.path so `app.*` resolves (the venv has no install).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.email.email_cleaner import is_acceptable_email
from app.engines.verification.location_verifier import _US_STATES


def _is_junk(email: str | None) -> bool:
    """The pool's own gate, with the empty string left alone.

    A blank email is 'not enriched yet', not junk — sweeping it would empty
    the pool instead of cleaning it.
    """
    return bool(email) and not is_acceptable_email(email)

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "output",
    "lead_research.db",
)

#: The SODA resource the TDLR connector writes to ``source_url``.
TDLR_RESOURCE = "%7358-krk7%"

#: The canonical USPS code set — the SAME table ``_parse_tdlr_row`` validates
#: against (``location_verifier._US_STATES`` is a ``{code: name}`` dict, so its
#: keys are the codes). Importing the parser's own table is the point: a second
#: copy would drift from the parser and silently start mis-classifying rows as
#: fragments again. Do not inline a list here.
_USPS_STATES: tuple[str, ...] = tuple(sorted(_US_STATES))

#: A TDLR row is a parse fragment when its ``state`` is a value the parser
#: cannot produce: MEMBERSHIP in the USPS set, never an equality test against
#: ``TX``. ``state != ''`` keeps the clause off rows whose state is simply
#: unknown (missing data is not corruption — see ``_is_junk``). Built once so
#: the SELECT and the DELETE cannot disagree about which rows they mean.
_TDLR_FRAGMENT_WHERE = (
    "source_url LIKE ? AND state != '' AND state NOT IN ({})".format(
        ", ".join("?" * len(_USPS_STATES))
    )
)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    purge_phone = "--purge-phone-emails" in sys.argv
    if dry_run:
        print("DRY RUN — every decision below is printed, nothing is deleted\n")
    if not os.path.exists(DB_PATH):
        print(f"no lead_research.db at {DB_PATH} — nothing to sweep")
        return 0
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        pending = conn.execute(
            "SELECT email, company, source_url, dork FROM pending_leads"
        ).fetchall()
        artifacts = [r for r in pending if _is_junk(r["email"])]
        print(f"pending_leads: total={len(pending)} "
              f"non-contact={len(artifacts)}")
        for r in artifacts:
            print(f"  DROP {r['email'][:60]}"
                  f" | {(r['company'] or '')[:30]}"
                  f" | {(r['source_url'] or '')[:60]}")
        if artifacts and not dry_run:
            conn.executemany(
                "DELETE FROM pending_leads WHERE email = ?",
                [(r["email"],) for r in artifacts],
            )
            conn.commit()
            print(f"deleted {len(artifacts)} pending non-contact rows")
        elif artifacts:
            print(f"WOULD delete {len(artifacts)} pending non-contact rows")

        dossiers = conn.execute(
            "SELECT email, domain, trade FROM dossiers"
        ).fetchall()
        d_artifacts = [r for r in dossiers if _is_junk(r["email"])]
        print(f"dossiers: total={len(dossiers)} non-contact={len(d_artifacts)}"
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
                    f"WHERE {_TDLR_FRAGMENT_WHERE}",
                    (TDLR_RESOURCE, *_USPS_STATES),
                ).fetchall()
                print(f"phone_leads (TDLR): parse-fragment rows={len(bad)}")
                for r in bad[:15]:
                    print(f"  DROP #{r['id']} {r['trade']}/{r['state']!r}"
                          f" | {(r['city'] or '')[:12]}"
                          f" | {(r['business_name'] or '')[:34]}")
                if len(bad) > 15:
                    print(f"  ... and {len(bad) - 15} more")
                if bad and not dry_run:
                    phones.execute(
                        f"DELETE FROM phone_leads WHERE {_TDLR_FRAGMENT_WHERE}",
                        (TDLR_RESOURCE, *_USPS_STATES),
                    )
                    phones.commit()
                    print(f"deleted {len(bad)} TDLR parse-fragment rows "
                          "(re-stocked correctly on the next phones pass)")
                elif bad:
                    print(f"WOULD delete {len(bad)} TDLR parse-fragment rows")

                # A junk email on a phone record is a real defect the user
                # reported ("phone number pr jo emails hain wo theek nahi
                # hain") — and it is ALSO why the replacement is a flag
                # rather than the default. Two different repairs are
                # possible and they are not equivalent:
                #
                #   email='' alone            -> enriched_at stays stamped,
                #     so the worker never retries and the row is 'honest
                #     empty' forever. WRONG: the address exists, we simply
                #     failed to read it (a phone welded onto it by
                #     unseparated text extraction).
                #   email='' + enriched_at='' -> the row re-queues and the
                #     (now fixed) enricher reads the page again.
                #
                # ``--purge-phone-emails`` does the second. It is opt-in
                # because re-enrichment spends search credits and SMTP
                # probes, so the founder decides when that bill is paid.
                with_email = phones.execute(
                    "SELECT id, email, email_source, business_name "
                    "FROM phone_leads WHERE email != ''"
                ).fetchall()
                junk = [r for r in with_email if _is_junk(r["email"])]
                print(f"phone_leads: with-email={len(with_email)} "
                      f"non-contact={len(junk)}")
                for r in junk[:15]:
                    print(f"  REVIEW #{r['id']} {r['email'][:50]}"
                          f" | src={r['email_source'] or '(none)'}"
                          f" | {(r['business_name'] or '')[:30]}")
                if len(junk) > 15:
                    print(f"  ... and {len(junk) - 15} more")
                if junk and purge_phone and not dry_run:
                    phones.executemany(
                        "UPDATE phone_leads SET email = '', email_source = '',"
                        " enriched_at = '',"
                        " updated_at = strftime('%Y-%m-%dT%H:%M:%f', 'now')"
                        " WHERE id = ?",
                        [(r["id"],) for r in junk],
                    )
                    phones.commit()
                    print(f"cleared {len(junk)} junk emails and re-queued "
                          "those rows for enrichment")
                elif junk and purge_phone:
                    print(f"WOULD clear {len(junk)} junk emails and re-queue "
                          "those rows")
                elif junk:
                    print("(report only — pass --purge-phone-emails to clear "
                          "them and re-queue those rows for enrichment)")
            finally:
                phones.close()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
