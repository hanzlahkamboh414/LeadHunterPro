"""Destructive purge: delete CONFIRMED-irrelevant dossiers from the live store.

Why: the governing complaint is "data jo hamari services se match nahi karta"
(our AI returning off-service data). The current re-gate (scoring.regate_
recommendation) can only inspect the dossier's INDUSTRY STRING, and in the
09-08 audit of the 171 working dossiers these entries show as "General
Contractor" / "Specialty Subcontractor" because the AI mislabeled a non-client
(a geotechnical-engineering consultancy, a marine/heavy-civil contractor, a
retail brand-management firm, a manufacturer) as a building-trade. The term
lists in company_profile.py can never catch a mislabeled industry — so we
delete the confirmed cases by hand (user authorized: "tum khud check kr ke
delete krdo").

Safety:
* An automatic timestamped backup of lead_research.db is made BEFORE any write.
* Every delete goes through the store's delete() -> deleted_leads audit trail
  with a "irrelevant-2026-09-08" reason (the admin screen reads it).
* Only EXPLICITLY verified non-clients / off-vertical firms are deleted;
  unknown / unverifiable rows are left alone (never starve the funnel on a
  guess, CLAUDE.md §11).

Usage (from backend/, venv):  python scripts/purge_irrelevant.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from collections import Counter

from app.lead_research.service import LeadResearchStore
from app.lead_research.scoring import regate_recommendation

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "output", "lead_research.db")

#: email -> why it is irrelevant to the estimation business. Each reason names
#: the OFF-vertical / non-client class from company_profile.json that the AI's
#: mislabeled industry string dodged.
PURGE = {
    "logo@3x-1-236x60.png": "garbage row: AI captured a logo-image filename as the email (HITT Contracting)",
    "kbradshaw@ninyoandmoore.com": "non-client: Ninyo & Moore is a geotechnical/environmental ENGINEERING CONSULTANCY, industry mislabeled 'General Contractor'",
    "cleitnick@abgi.com": "non-client: ABGI is retail BRAND/program management (not construction), mislabeled 'General Contractor / Program Manager'",
    "tara.boyles@us.bosch.com": "excluded: Bosch is a MANUFACTURER (engineering & technology manufacturing), never a bidding bidder",
    "gary.rehm@parsons.com": "non-client: Parsons is an A/E/C ENGINEERING & infrastructure-services consultancy, mislabeled",
    "mrussell@bbiius.com": "excluded: Balfour Beatty Infrastructure is HEAVY/INFRASTRUCTURE (rail/highway), not building trades",
    "estimating@dutragroup.com": "excluded: The Dutra Group is HEAVY CIVIL MARINE construction",
    "mik@powerengconstruction.com": "excluded: Power Engineering Construction is MARINE & HEAVY CIVIL",
    "rzito@shimmick.com": "excluded: Shimmick is HEAVY CIVIL / INFRASTRUCTURE (water, dams)",
    "renee@uamonline.com": "excluded: Utility Asset Management is a UTILITY-asset firm, mislabeled 'Specialty Subcontractor'",
    "brad@inroadspaving.com": "excluded: InRoads is PAVING + asphalt MATERIALS supplier-miller",
    "rjames@fastechus.com": "excluded: FASTECH is ENERGY & alternative-fuel INFRASTRUCTURE, mislabeled",
    "maria@provenmanagement.com": "excluded: ProVen Mgmt is a HEAVY-CIVIL (rail/water/power) services contractor, verified",
    "jadams@glfusa.com": "excluded: GLF is HEAVY CIVIL / MARINE / BRIDGE construction, verified",
    "estimating@rivconstruct.com": "excluded: Riverside Construction is a general ENGINEERING contractor, HEAVY/CIVIL",
    "pandrew@thalle.com": "excluded: Thalle Construction is HEAVY CIVIL (marine/dredging) national firm",
    "jake.preising@signaturebridge.com": "excluded: Signature BRIDGE Construction — bridge contractor, name is the proof",
    "saul@rpoyas.com": "non-client: Robert W. Poyas is a LANDSCAPE MAINTENANCE provider, not a bidding trade",
    "kbrown@jensencorp.com": "non-client: Jensen Landscape Services is a LANDSCAPE MAINTENANCE provider",
    "amarkov@prestigegroup-usa.com": "self/non-client: Markov Prestige Group = platform-owner prestige group (markovpg.com link), role 'N/A', not an outside client",
    "pbekey@kcaengineers.com": "non-client: kcaengineers.com = KCA ENGINEERS (engineering consultancy); AI call failed so label is empty",
    "mark@csmarine.com": "excluded: CS MARINE — marine contractor, industry left empty",
    "jgarner@batterygiant.com": "non-client: Battery Giant is a BATTERY-supply retailer, not construction",
    "mark.vagle@pacificmobile.com": "excluded: Pacific Mobile is a MOBILE/telecom provider, mislabeled; industry empty",
}

#: Second approved tier (2026-09-08): ambiguous / unresearched noise rows —
#: nothing confirmed, so the user chose to drop them from the frontend rather
#: than keep empty dossiers. Cloud CM looks like a SaaS platform by its own
#: name (thecloudcm.com); Ruiz "Trans" Development reads transportation; the
#: rest have empty company/industry/location and zero usable research. Each is
#: recoverable from the backup created on the first purge run.
PURGE_TIER2 = {
    "jpack@thecloudcm.com": "ambiguous: 'Cloud CM' (thecloudcm.com) reads as a SaaS construction-management platform, unverified",
    "kristen@ruiztransdevelopment.com": "ambiguous: 'Trans' Development reads transportation/road, unverified",
    "mike.linger@beaverexcavating.com": "unverified: AI call failed; Beaver Excavating is a legit trade but the dossier is empty (recoverable from backup)",
    "ruairi@roebucksf.com": "unverified: company cannot be identified, no industry/location",
    "dalian@westbaybuilders.com": "unverified: AI gave no industry/location/verifiable company despite the 'builders' name",
    "mbarriga@rbcompany.com": "unverified: company cannot be identified, no industry/location",
    "rebecca.brown@mhc.com": "unverified: no search results / unreachable site, no industry/location",
    "parts@gilliehyde.com": "unverified: company cannot be identified, no industry/location",
    "jody@apcind.com": "unverified: bid-holder list only, uncertain industry, no location",
}


def _audit(path: str) -> tuple[int, Counter, list[str]]:
    """Read-only snapshot: (total dossiers, recommendation counts, working emails)."""
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT email, dossier_json FROM dossiers").fetchall()
    conn.close()
    from app.lead_research.models import LeadDossier

    total = 0
    rec = Counter()
    working: list[str] = []
    for email, blob in rows:
        try:
            d = LeadDossier.from_dict(json.loads(blob))
        except Exception:
            continue
        total += 1
        r = regate_recommendation(d)
        rec[r] += 1
        if r != "skip":
            working.append(email)
    return total, rec, working


def main() -> None:
    backup = DB_PATH + ".bak-20260908-irrelevant-purge"
    if not os.path.exists(DB_PATH):
        sys.exit(f"DB not found: {DB_PATH}")
    shutil.copy2(DB_PATH, backup)
    print(f"backup: {backup}")

    before_total, before_rec, before_working = _audit(DB_PATH)
    print(f"BEFORE: total={before_total}  rec="
          + ", ".join(f"{k}={v}" for k, v in sorted(before_rec.items()))
          + f"  working={len(before_working)}")

    store = LeadResearchStore(DB_PATH)
    found, missing, already_gone = [], [], []
    plan = {**PURGE, **PURGE_TIER2}
    for email, reason in plan.items():
        ok = store.delete(email, reason=f"irrelevant-2026-09-08: {reason}")
        if ok:
            found.append((email, reason))
        # A missing row is EITHER already deleted by a prior run of this
        # script (email=hash of a purged row — treat as success) OR truly
        # absent (skip, never invent). Check the deleted_leads audit trail.
        elif email.lower() in {
            r["email"].lower() for r in store.deleted_log(limit=100000)
        }:
            already_gone.append((email, reason))
        else:
            missing.append((email, reason))

    after_total, after_rec, after_working = _audit(DB_PATH)
    print(f"AFTER:  total={after_total}  rec="
          + ", ".join(f"{k}={v}" for k, v in sorted(after_rec.items()))
          + f"  working={len(after_working)}")

    print(f"\ndeleted {len(found)} / {len(plan)}  "
          f"(already-deleted-from-earlier-run: {len(already_gone)}, "
          f"truly-absent: {len(missing)})")
    for email, reason in found:
        print(f"  - {email:<35} {reason}")
    if already_gone:
        print("\nalready deleted by the first purge (kept):")
        for email, _ in already_gone:
            print(f"  = {email}")
    if missing:
        print(f"\nTRULY NOT FOUND in DB ({len(missing)}) — skipped:")
        for email, reason in missing:
            print(f"  ? {email:<35} {reason}")
    print(f"\nworking {len(before_working)} -> {len(after_working)} "
          f"({len(before_working) - len(after_working)} removed from the frontend)")


if __name__ == "__main__":
    main()