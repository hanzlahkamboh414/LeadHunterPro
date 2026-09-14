"""Read-only dump of EVERY dossier the frontend currently shows (working).

A working dossier is one whose re-gate is NOT skip. This prints each one with
enough detail to judge relevance against what we sell (construction estimation
for active building-trades bidders): email, company, industry, location, score,
role, bound, sources, and a headline fact. Writes NOTHING.

Usage (from backend/, venv):  PYTHONPATH=. python scripts/dump_working.py [recommendation]
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

from app.lead_research.scoring import regate_recommendation

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "output", "lead_research.db")
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    import sqlite3
    conn = sqlite3.connect(uri, uri=True)
    rows = conn.execute("SELECT email_hash, email, dossier_json FROM dossiers").fetchall()
    conn.close()

    from app.lead_research.models import LeadDossier
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    dossiers = []
    for _eh, email, blob in rows:
        try:
            d = LeadDossier.from_dict(json.loads(blob))
        except Exception:
            continue
        rec = regate_recommendation(d)
        if rec == "skip":
            continue
        if want and rec != want:
            continue
        dossiers.append((email, d, rec))

    dossiers.sort(key=lambda t: (0 if t[2] == "contact_now" else 1,
                             -(t[1].potential_score or 0)))

    print(f"WORKING dossiers shown on frontend: {len(dossiers)}  (rec filter: "
          f"{want or 'all'})\n")
    ind_counts = Counter(d.company.industry or "(none)" for _, d, _ in dossiers)
    print("industries:", ", ".join(f"{k} x{v}" for k, v in ind_counts.most_common()))
    print()
    for email, d, rec in dossiers:
        roles = d.person.role or ""
        head = ""
        if d.company.facts:
            head = " | " + (d.company.facts[0].claim or "")[:90]
        print(f"  [{rec:<12} {d.potential_score:>3.1f}] {d.company.name or '-':<30} "
              f"| ind:{d.company.industry or '-':<34} loc:{d.company.location or '-':<20} "
              f"| role:{roles[:22]:<22} bound={d.person.bound}")
        src_hint = (d.sources_checked[0] if d.sources_checked else "-")[:30]
        print(f"      {email}  src:{src_hint:<30}  "
              f"is_client:{d.company.is_our_client or '-':<5}  {head}")


if __name__ == "__main__":
    main()