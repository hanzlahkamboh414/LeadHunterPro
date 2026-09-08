"""Read-only audit: how much IRRELEVANT (non-client) data is showing as working?

The user's governing complaint — "data jo hamari company ki services se match
nahi karta" — is checked here with a re-gate that is ONE rule ahead of the
current ``regate_recommendation``:

  CURRENT  regate_recommendation → hides  non-construction, off-vertical,
                                    low-score / unbound / irrelevant-role.
  PROPOSED + is_non_client(industry)  → ALSO hides A/E/C consultants, software/
                                    IT, transit/mobility, associations, etc.

A dossier that is "working under CURRENT" but "skip under PROPOSED" is
irrelevant data currently showing on the frontend — this script counts and
lists it before the fix is applied. It writes NOTHING (mode=ro).

Usage (from backend/, venv):  python scripts/audit_irrelevant_leads.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter

from app.company_profile import get_profile
from app.lead_research.scoring import regate_recommendation

# Windows consoles default to cp1252 and choke on non-ASCII; force UTF-8 so the
# report never crashes mid-print (read-only tool — output only).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _connect_ro(path: str) -> sqlite3.Connection:
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _load_dossiers(path: str) -> list:
    conn = _connect_ro(path)
    dossiers = []
    try:
        rows = conn.execute(
            "SELECT email_hash, email, dossier_json FROM dossiers"
        ).fetchall()
    finally:
        conn.close()
    from app.lead_research.models import LeadDossier

    for eh, email, blob in rows:
        try:
            dossiers.append((eh, email, LeadDossier.from_dict(json.loads(blob))))
        except Exception:
            pass  # corrupt row — never crashes the audit
    return dossiers


def main() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "output", "lead_research.db")
    print(f"DB: {os.path.abspath(path)}  (read-only)\n")
    dossiers = _load_dossiers(path)
    print(f"dossiers loaded: {len(dossiers)}")

    prof = get_profile()
    irrelevant: list[tuple] = []  # (email, company, industry, reason)
    working_current = working_proposed = 0
    rec_counts: Counter = Counter()
    for eh, email, d in dossiers:
        cur = regate_recommendation(d)
        rec_counts[cur] += 1
        if cur != "skip":
            working_current += 1
            ind = d.company.industry or ""
            if ind and prof.is_non_client(ind):
                tie = next((t for t in prof.non_client_terms if t in ind.lower()), "")
                irrelevant.append((email, d.company.name, ind, tie))
            else:
                working_proposed += 1

    print(f"\nrecommendations (CURRENT re-gate): "
          + ", ".join(f"{k}={v}" for k, v in rec_counts.items()))

    n = len(irrelevant)
    print(f"\nWORKING under current rules: {working_current}")
    print(f"  |-- of those, IRRELEVANT (non-client term match): {n}")
    print(f"  L-- stays working under PROPOSED rule: {working_proposed}")
    print(f"\n>>> {n} irrelevant dossier(s) would be REMOVED from the frontend "
          f"by adding is_non_client() to the re-gate.")

    if irrelevant:
        print(f"\nsample (first 15):")
        by_reason = Counter(r[3] for r in irrelevant)
        for r in sorted(irrelevant, key=lambda x: x[3])[:15]:
            print(f"  [{r[3] or '?':<22}] {r[1] or '-':<28} {r[2]:<40} {r[0]}")
        print(f"\nreason breakdown: " + ", ".join(f"{k}={v}" for k, v in by_reason.most_common()))


if __name__ == "__main__":
    main()