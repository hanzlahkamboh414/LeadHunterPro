"""One pass of the AI Source Scout's trust pipeline (P9 maintenance CLI).

Run (from backend/):
    python scripts/scout_pass.py            # full pass (playbook + verify + probation)
    python scripts/scout_pass.py --no-ai    # mechanical verify of pending only
    python scripts/scout_pass.py --resurrect SOURCE_ID   # clear a yield drop

Stages, in order (staged trust — a source advances one stage per pass, so
full promotion takes several runs spaced over time; a pass RATE only means
something when trials are spread out):

  1. SEED    the known/dead memory (idempotent — public SearXNG 429s, DDG
             lite, CSLB's F5 WAF, OSM Overpass dead; WA/TDLR good)
  2. PROPOSE the AI playbook proposes new SODA candidate sources into
             quarantine (flood-guarded at 12 pending; never re-proposes a
             known-dead route or an existing source_id)
  3. VERIFY  every ``proposed`` source gets the mechanical verifier (real
             fetch, error classify + DNS retry + IP pin + alt endpoint,
             shape/trade-evidence/volume/recency). Pass -> ``verified``;
             fail stays ``proposed`` (retryable).
  4. PROBATION  one agnes check per ``verified``/``probation`` source (the
             mechanical gate runs first — no AI spend without a logged
             fetch), auto-promote at >= 70% after 5 trials, auto-retire
             below it. No admin anywhere.

  5. YIELD   the phones lane's source-level yield table (P10): trials /
             working per (source, segment) and which drops are active.
             ``--resurrect`` clears one (human-only, never auto-promoted).

Promoted sources are picked up by the phones lane's
``soda.effective_trade_coverage()`` on its next lookup — nothing else to
wire. Honest JSON summaries per stage, nothing silent (CLAUDE.md §6).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.harvester.store import HarvesterStore  # noqa: E402
from app.source_scout import proposals as prop_mod  # noqa: E402
from app.source_scout import verifier as vf  # noqa: E402
from app.source_scout import probation as pb  # noqa: E402
from app.source_scout.store import ScoutStore  # noqa: E402


def _ai_ask():
    """The scout's LLM transport (its own key lane, AI_API_KEY_3)."""
    from app.ai.gateway import make_ai_ask
    from app.core.config import settings

    return make_ai_ask(api_key=settings.AI_API_KEY_3)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db", default=None,
        help=("Scout db path (default: the live "
              "backend/output/source_scout.db)"),
    )
    parser.add_argument(
        "--no-ai", action="store_true",
        help=("Mechanical verify of pending proposals only — no playbook "
              "generation, no probation checks (zero AI spend)"),
    )
    parser.add_argument(
        "--resurrect", default="", metavar="SOURCE_ID",
        help=("Clear a phones-lane source-yield drop (P10) so the source is "
              "retried — HUMAN-ONLY, never auto-promoted"),
    )
    parser.add_argument(
        "--segment", default="",
        help=("The yield segment to clear with --resurrect "
              "('trade | ST', default '' = the GLOBAL row)"),
    )
    args = parser.parse_args()

    store = ScoutStore(db_path=args.db) if args.db else ScoutStore()
    ai_ask = None if args.no_ai else _ai_ask()

    # -- 1. known/dead memory (idempotent) ---------------------------------
    prop_mod.seed_known(store)
    print("KNOWN/DEAD:", json.dumps(
        {sid: k["status"] for sid, k in store.known_map().items()},
        sort_keys=True))

    # -- 2. playbook proposals (AI; skipped with --no-ai) -------------------
    if ai_ask is None:
        print("PROPOSE: skipped (--no-ai)")
    else:
        out = prop_mod.generate_source_proposals(store, ai_ask=ai_ask)
        print("PROPOSE:", json.dumps({
            "proposed": out["proposed"], "rejected": out["rejected"],
            "reason": out["reason"],
        }))

    # -- 3. mechanical verify of every pending proposal ---------------------
    verified: list[str] = []
    failed: list[dict[str, str]] = []
    for row in store.list_status("proposed"):
        out = vf.verify_source(store, row["source_id"])
        if out["passed"]:
            verified.append(row["source_id"])
        else:
            failed.append({"source_id": row["source_id"],
                           "reason": out["reason"][:200]})
    print("VERIFY:", json.dumps({"verified": verified, "failed": failed}))

    # -- 4. agnes probation (AI; skipped with --no-ai) ----------------------
    if ai_ask is None:
        print("PROBATION: skipped (--no-ai)")
    else:
        summary = pb.probation_pass(store, ai_ask=ai_ask)
        print("PROBATION:", json.dumps(summary))

    live = [
        {"source_id": r["source_id"], "status": r["status"]}
        for r in store.list_status("promoted")
    ]
    print("PROMOTED (serving):", json.dumps(live))

    # -- 5. phones-lane source yield (P10) -----------------------------------
    harvester = HarvesterStore()
    if args.resurrect:
        harvester.delete_source_yield(args.resurrect, args.segment)
        print(f"RESURRECT: cleared yield row "
              f"{args.resurrect!r} segment={args.segment!r} — the source is "
              f"retried on the next pick")
    yield_rows = harvester.source_yield_all()
    print("PHONE SOURCE YIELD:", json.dumps(yield_rows, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
