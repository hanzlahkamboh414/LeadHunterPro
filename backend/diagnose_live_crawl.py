"""Diagnostic — why does the API-free DirectoryCrawlSource return 0 live?

The Inc9d live probe showed ``directory_crawl empty results=0`` on a real
machine (not the DNS-blocked sandbox). This script isolates the source to
find the exact failure point (CLAUDE.md §7, §12):

- does SourcePlanner actually plan Texas crawlable seeds?
- does the crawler reach them (pages_attempted vs fetched)?
- are pages rejected by the extractor/classifier (reject reasons at DEBUG)?

Read-only: it only runs the existing production source and prints its own
stats. No production code is touched and nothing is modified.
"""

from __future__ import annotations

import logging

from app.discovery.sources.directory_crawl_source import DirectoryCrawlSource
from app.engines.source_intelligence.source_models import SourcePlannerRequest
from app.engines.source_intelligence.source_planner import SourcePlanner

# Show the source's per-page accept/reject reasons (and why a seed was
# skipped) without flooding the console with raw crawler traffic.
logging.basicConfig(level=logging.WARNING)
logging.getLogger("app.discovery.sources.directory_crawl_source").setLevel(logging.DEBUG)


def main() -> int:
    planner = SourcePlanner()
    planned = planner.plan(
        SourcePlannerRequest(
            industry="Roofing", country="USA", state="TX", city="Dallas"
        )
    )
    print("=== Planned sources (SourcePlanner) ===")
    for r in planned.sources:
        print(
            f"  [prio={r.priority:>3} crawlable={r.supports_company_discovery}] "
            f"{r.name:<46} {r.url}"
        )
    print(f"  total={planned.total_sources} skipped={planned.skipped_sources}\n")

    status, companies, meta = DirectoryCrawlSource().discover(
        industry="Roofing", location="Dallas Texas", limit=20
    )
    print("=== DirectoryCrawlSource.discover ===")
    print(f"status={status.value}  companies={len(companies)}")
    for key in (
        "reason",
        "seeds_planned",
        "seeds_attempted",
        "pages_attempted",
        "pages_fetched",
        "fetch_failures",
        "companies_accepted",
        "companies_rejected",
    ):
        if key in meta:
            print(f"  {key}: {meta[key]}")
    if meta.get("error"):
        print(f"  error: {meta['error']}")

    print("\n=== Companies found ===")
    for c in companies:
        print(
            f"  - {c.get('company_name')} | {c.get('website')} | "
            f"{c.get('city')}, {c.get('state')} | prov={c.get('data_provenance')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
