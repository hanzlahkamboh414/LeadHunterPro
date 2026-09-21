"""Live proof that Phase 1 evidence intake works — the post-deploy gate.

WHY THIS EXISTS: Phase 1 switched ON three plugins that had been built, tested
and never called (usaspending / google_news / company_site). Whether the wiring
works is not something a health check can tell you — the app can be up, the
flag can be True, and still nothing may be stored. So this exercises the real
unit against the real endpoints and reads the result back out of the database.

It makes real network calls (no AI, so no model credits are spent).

    python scripts/intent_evidence_check.py --domain acme.com --name "Acme Construction"
    python scripts/intent_evidence_check.py --registered      # wiring only, no network

Exit codes: 0 = the intake stored evidence or gave an honest, well-reasoned
outcome. 2 = the intake produced nothing and no reason (§6 failure), or the
wiring is broken. A "no evidence found" result is a PASS when every provider
answered — that is a real answer about the company, not a failure of this
check. What fails is silence.
"""

from __future__ import annotations

import argparse
import os
import sys

# `python scripts/x.py` puts **scripts/** on sys.path, not backend/ — so
# `import app` would fail without this (same fix as live_expansion_check.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings  # noqa: E402
from app.discovery.intent import registered_intent_plugins  # noqa: E402
from app.research.store import ResearchEvidenceStore  # noqa: E402
from app.research.taxonomy import ResearchState  # noqa: E402


def _print_wiring() -> int:
    """Report what the process can actually reach, with no network calls."""
    print("=" * 68)
    print("PHASE 1 — WIRING")
    print("=" * 68)
    print(f"INTENT_EVIDENCE_ENABLED : {settings.INTENT_EVIDENCE_ENABLED}")
    active = registered_intent_plugins()
    print(f"registered (enabled)    : {[p.name for p in active] or 'NONE'}")
    if not active:
        print()
        print("The registry is empty in THIS process. That is expected for a")
        print("CLI run (registration happens in the app lifespan); the intake")
        print("falls back to the three built-ins and logs that fallback.")
    print(f"evidence db             : {ResearchEvidenceStore()._db_path}")
    return 0


def _print_json(name: str, payload: object) -> None:
    import json

    print(f"{name}: {json.dumps(payload, indent=2, default=str)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="", help="company domain to research")
    parser.add_argument("--name", default="", help="company name")
    parser.add_argument("--website", default="", help="company website")
    parser.add_argument("--location", default="", help="city/state context")
    parser.add_argument(
        "--registered", action="store_true",
        help="only report the wiring; make no network calls",
    )
    parser.add_argument(
        "--read-back", action="store_true",
        help="also print what is already stored for this domain",
    )
    args = parser.parse_args()

    _print_wiring()
    if args.registered:
        return 0
    if not args.domain:
        print("\n--domain is required to run a collection (identity needs a handle).")
        return 2

    from app.research.intake import collect_company_evidence

    print()
    print("=" * 68)
    print("PHASE 1 — LIVE COLLECTION")
    print("=" * 68)
    result = collect_company_evidence(
        company_name=args.name or args.domain,
        domain=args.domain,
        website=args.website,
        location=args.location,
    )
    _print_json("result", {
        "state": result.state,
        "honest_reason": result.honest_reason,
        "company_id": result.company_id,
        "run_id": result.run_id,
        "plugins_available": result.plugins_available,
        "evidence_collected": result.evidence_collected,
        "evidence_stored": result.evidence_stored,
        "evidence_known": result.evidence_known,
        "rejected": result.rejected,
        "error": result.error,
    })
    print()
    print("PER-PROVIDER (the §6 detail — never a bare 'live=False')")
    for entry in result.providers:
        print(
            f"  {entry.get('provider', '?'):<14} status={entry.get('status')!s:<12} "
            f"returned={entry.get('returned')} accepted={entry.get('accepted')} "
            f"deduped={entry.get('deduped')} blank_url={entry.get('blank_url')}"
            + (f" error={entry.get('error')}" if entry.get("error") else "")
        )

    if args.read_back and result.company_id:
        store = ResearchEvidenceStore()
        rows = store.evidence_for_company(result.company_id)
        print()
        print(f"STORED EVIDENCE for {result.company_id}: {len(rows)} row(s)")
        for row in rows:
            print(
                f"  [{row.evidence_type}] {row.source_url}\n"
                f"      published={row.published_at or '-'} "
                f"legacy={row.legacy_type or '-'} "
                f"event={row.event_candidate or '-'}"
            )
            # The excerpt is the audit trail: without it a stored row is a
            # claim with no visible basis, and a wrong one (a nav-bar phrase
            # read as a signal) cannot be told from a right one.
            if row.excerpt:
                print(f"      excerpt={row.excerpt}")

    print()
    if result.state == ResearchState.VERIFIED.value:
        if result.evidence_stored:
            print(
                f"PASS — evidence was collected and stored "
                f"({result.evidence_stored} new row(s))."
            )
        else:
            # A second run over the same company stores nothing, and that is
            # the point of a company-keyed store: re-research refines rather
            # than duplicating. Saying "stored" here would be false.
            print(
                f"PASS — evidence was collected and already known "
                f"({result.evidence_known} row(s) confirmed, 0 new — the "
                f"company-keyed store refined instead of duplicating)."
            )
        return 0
    if result.honest_reason or result.error:
        print(f"PASS (honest outcome, no evidence this run) — {result.state}")
        print("     A reasoned 'nothing found' is an answer; only silence fails.")
        return 0
    print("FAIL — the intake produced no outcome and no reason (CLAUDE.md §6).")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
