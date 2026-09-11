"""Off-vertical leak report — how many stored dossiers fail the company-profile
boundary (the "fiber construction" class: telecom/utility/road/pipeline/materials).

Run (from backend/):
    python scripts/leak_report.py

READ-ONLY — never mutates dossiers (CLAUDE.md §6: an honest count, no silent
change). For each dossier that passes the binary construction check but matches
an excluded vernacular, it is reported as an off-vertical leak and you can
remove those in one Junk sweep (or per-lead Delete). Empty? The leak is closed.

Uses the SAME gate as ``regaterecommendation`` (app.lead_research.scoring), so
the report reflects today's verdicts exactly.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company_profile import get_profile  # noqa: E402
from app.lead_research.scoring import regate_recommendation  # noqa: E402
from app.lead_research.service import LeadResearchStore  # noqa: E402


def main() -> int:
    store = LeadResearchStore()
    profile = get_profile()

    dossiers = store.list_all()
    leaks = []
    for d in dossiers:
        ind = (d.company.industry or "").strip()
        if not ind:
            continue
        if profile.is_off_vertical(ind):
            rec = regate_recommendation(d)
            leaks.append((d, rec))

    off_by_tier: dict[str, int] = {}
    for _, rec in leaks:
        off_by_tier[rec] = off_by_tier.get(rec, 0) + 1

    print("OFF-VERTICAL LEAK REPORT")
    print("=" * 60)
    print(f"Dossiers scanned      : {len(dossiers)}")
    print(f"Off-vertical detected : {len(leaks)}")
    if leaks:
        print("  by current verdict  : " + ", ".join(
            f"{k}={v}" for k, v in sorted(off_by_tier.items())
        ))
        print("-" * 60)
        print("Sample (industry -> company -> verdict):")
        for d, rec in leaks[:20]:
            print(
                f"  [{rec:>11}] {d.company.industry!r} -> {d.company.name or '?'} "
                f"({d.email})"
            )
        if len(leaks) > 20:
            print(f"  ... and {len(leaks) - 20} more")
        print("-" * 60)
        print("These never show as actionable leads (re-gated to skip), and the")
        print("Junk sweep (POST /leads/clear-junk or the UI) removes them at once.")
    else:
        print("  NO off-vertical leaks — the vertical boundary is holding.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())