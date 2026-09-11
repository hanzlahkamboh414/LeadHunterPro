"""Phase H maintenance trigger — have the LLM propose NEW plan-holder dorks.

The self-learning loop (Phase F/G) can DROP dead dorks but never ADD new ones,
so discovery coverage is bounded by the 8 hand-written dorks. This script is the
**human-in-the-loop add-side**: it asks the LLM for 3-5 NEW dork phrasings that
target plan-holder / bid-roster PDFs the current set misses, sanitizes every
proposal (guards in :mod:`app.discovery.template_generation`), and stages the
survivors in the ``template_candidates`` table. They then enter the SAME Phase G
yield loop as first-class templates: dispatched until proven dead, auto-promoted
``active`` at ``working >= PROVEN_GOOD``, lazy-demoted ``dropped`` once the yield
says so — a generated dork is never injected as proven (§12).

HARD GUARD — cold-start: NO LLM call until some dispatched dork has reached
``MIN_TRIALS`` (12) real trials. The LLM must have yield evidence to improve on;
calling before that spends a credit to invent angles from nothing. When the guard
holds, the script reports the honest reason and exits 0 — no call, no credit.

Run (from backend/):
    python scripts/generate_templates.py [db_path]
    python scripts/generate_templates.py [db_path] --segment "roofing | dallas tx"

The ``--segment`` form (Phase I) targets generation at ONE (trade | location)
segment: the cold-start guard and the LLM evidence table read THAT segment's
yield, and the prompt asks for angles that fill that segment's gap. Without
``--segment``, the script prints the per-segment COVERAGE report (which dorks
are proven-dead / working / no-evidence per segment — the visibility that finds
coverage gaps) and runs the global Phase H generation as before.

IDEMPOTENT — ``propose()`` dedupes on the pattern PK, so a re-run stages nothing
twice and a guard-failed re-run makes no LLM call.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.discovery.sources.plan_holder_source import _DORK_TEMPLATES  # noqa: E402
from app.discovery.template_candidates import TemplateCandidateStore  # noqa: E402
from app.discovery.template_generation import generate_dork_candidates  # noqa: E402
from app.discovery.yield_learning import DiscoveryYieldStore, segment_key  # noqa: E402

#: The ``all()`` key separator between a template label and its segment
#: (mirrors ``yield_learning._SEGMENT_SEP`` — a global row has no separator).
_SEP = "|"


def _status_counts(candidate_store: TemplateCandidateStore) -> dict[str, int]:
    counts: dict[str, int] = {"candidate": 0, "active": 0, "dropped": 0}
    for row in candidate_store.all():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def _segments_and_rows(
    yield_store: DiscoveryYieldStore,
) -> tuple[dict[str, dict[str, dict]], set[str]]:
    """Split ``all()`` into ``{segment: {label: row}}`` + the set of segments.

    A global row (no ``|``) lands on ``segment=''``; a segmented row
    ``label|segment`` is split once on the first ``|`` (segment itself may carry
    ``" | "`` internally, e.g. ``roofing | dallas tx``).
    """
    seg_rows: dict[str, dict[str, dict]] = {}
    for key, row in yield_store.all().items():
        if _SEP in key:
            label, seg = key.split(_SEP, 1)
        else:
            label, seg = key, ""
        seg_rows.setdefault(seg, {})[label] = row
    return seg_rows, set(seg_rows)


def _coverage_report(
    yield_store: DiscoveryYieldStore,
    candidate_store: TemplateCandidateStore,
    *,
    only_segment: str = "",
) -> None:
    """Print, per segment, each dork's status: dead / working / probing / gap.

    This is the VISIBILITY side of Phase I — it shows which (trade, location)
    segment has a coverage gap (dorks with no evidence at all) versus where
    discovery is working. ``only_segment`` limits the report to one segment.
    """
    seg_rows, segments = _segments_and_rows(yield_store)
    known = set(_DORK_TEMPLATES) | {r["label"] for r in candidate_store.all()}
    ordered = sorted(segments, key=lambda s: (s == "", s))
    if only_segment:
        ordered = [s for s in ordered if s == only_segment] or [only_segment]

    print("\nPER-SEGMENT DISCOVERY COVERAGE")
    print("-" * 60)
    for seg in ordered:
        rows = seg_rows.get(seg, {})
        print(f"\n[segment {seg!r}]")
        any_label = False
        for label in sorted(known):
            row = rows.get(label)
            if row is None:
                if yield_store.should_skip(label, seg):
                    status = "dead (global drop)"
                else:
                    status = "NO EVIDENCE (coverage gap)"
            elif row["working"] > 0:
                status = f"WORKING trials={row['trials']} working={row['working']}"
            elif yield_store.should_skip(label, seg):
                status = f"dead trials={row['trials']}"
            else:
                status = f"probing trials={row['trials']} working=0"
            print(f"  {label}: {status}")
            any_label = True
        if not any_label:
            print("  (no dork has dispatched for this segment yet — a coverage gap)")
    print("-" * 60)


def _generation_report(
    result: dict, candidate_store: TemplateCandidateStore, segment: str,
) -> None:
    print("=" * 60)
    if segment:
        print(f"LLM template generation (targeted: {segment!r})")
    else:
        print("LLM template generation (global)")
    if result["reason"]:
        print(f"GUARD/STATUS: {result['reason']}")
    print(f"generated: {len(result['generated'])}")
    for d in result["generated"]:
        print(f"  + {d}")
    print(f"rejected:  {len(result['rejected'])}")
    for r in result["rejected"]:
        print(f"  - {r}")
    counts = _status_counts(candidate_store)
    print("candidate lifecycle:")
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}")
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM dork-template generation")
    parser.add_argument("db_path", nargs="?", default=None,
                        help="path to the lead_research.db (default: output/)")
    parser.add_argument("--segment", default="",
                        help='target ONE segment, e.g. "roofing | dallas tx" '
                             "(lowercase, space-normalized)")
    args = parser.parse_args()

    db_path = args.db_path
    yield_store = DiscoveryYieldStore(db_path)
    candidate_store = TemplateCandidateStore(db_path)

    print("Phase H/I — plan-holder dork template generation")
    if args.segment:
        # The CLI lets the user type a friendly segment; normalize it through
        # the SAME key the pipeline derives, so the evidence matches exactly.
        parts = args.segment.split("|", 1)
        trade = parts[0].strip() if parts else ""
        location = parts[1].strip() if len(parts) > 1 else ""
        segment = segment_key(trade, location)
        _coverage_report(yield_store, candidate_store, only_segment=segment)
        result = generate_dork_candidates(
            yield_store, candidate_store=candidate_store, segment=segment,
        )
        _generation_report(result, candidate_store, segment)
    else:
        _coverage_report(yield_store, candidate_store)
        result = generate_dork_candidates(yield_store, candidate_store=candidate_store)
        _generation_report(result, candidate_store, "")

    print("Generated dorks now dispatch through the Phase G/I yield loop — they")
    print("earn their place (working>=3 -> active) or die on evidence, never by")
    print("being proposed. Re-run is safe: propose() dedupes on the pattern.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
