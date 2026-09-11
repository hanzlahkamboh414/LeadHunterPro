"""Live verification: run the exact Houston discovery query that 0-resulted.

Job 49aae0e95f73 (General Contractors / Houston TX, 2026-09-07 12:25:52)
completed with 0 results and the misleading reason ``no_sources_registered``.
This runs the SAME query through the real multi-source orchestrator and prints
the honest per-source status + fallback_reason + whether any records/PDFs were
found, proving both that the pipeline yields data and that failures are now
diagnosable (source_stats in meta, honest fallback_reason).

SearXNG is priority 10 (free, local): if it answers, Tavily (paid) is not hit.
"""

from __future__ import annotations

import json
import sys

from app.discovery.sources.status import SourceStatus
from app.leads.pipeline import _source_stats_for_log, run_discovery


def main() -> int:
    status, records, meta = run_discovery(
        "General Contractors", "Houston TX", limit=40
    )
    print("\n=== HOUSTON LIVE DISCOVERY ===")
    print(f"status          : {status.value}")
    print(f"data_source     : {meta.get('data_source')}")
    print(f"fallback_reason : {meta.get('fallback_reason') or '(none — live)'}")
    print(f"records         : {len(records)}")
    print(f"pdf_urls        : {len(meta.get('pdf_urls') or [])}")
    print(f"source_stats    : {json.dumps(_source_stats_for_log(meta))}")
    print("=== PER-SOURCE RAW ===")
    for name, s in (meta.get("source_stats") or {}).items():
        st = s.get("status")
        print(
            f"  {name}: {getattr(st, 'value', st)}  results={s.get('results', 0)}"
            f"  reason={(s.get('metadata') or {}).get('reason', '')}"
        )
    if records:
        print("\nSample records:")
        for r in records[:3]:
            print(
                f"  - {r.get('company_name')}  {r.get('website')}"
                f"  src={r.get('data_provenance')}"
            )
    print(f"\n>>> {'YIELDS DATA' if records else 'NO RECORDS'} ({status.value})")
    return 0 if records else 1


if __name__ == "__main__":
    sys.exit(main())
