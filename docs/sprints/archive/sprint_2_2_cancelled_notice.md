# Sprint 2.2 Connector SDK — Archived

**Archived:** 2026-08-03  
**Replaced by:** [sprint_2_2_real_discovery.md](../sprint_2_2_real_discovery.md)  
**Status:** CANCELLED

---

This sprint document is archived because Manual QA revealed that its scope did not address the core architectural failure in LeadHunter Pro's discovery pipeline.

## Why It Was Cancelled

The original Sprint 2.2 scoped SDK polish (interface finalization, test coverage, API routes, documentation) while the actual discovery engine returned empty results for trade-specific queries like "Roofing Texas."

A comprehensive engineering review found:

1. **All connectors filter static fixtures** — zero live data fetching
2. **Only ~40 companies exist** in the entire dataset
3. **Industry matching is too narrow** — "roofing" matches nothing
4. **Two parallel SDK systems** create confusion and maintenance debt
5. **No crawler infrastructure** exists for future live sources

Finishing this sprint would have delivered a well-documented framework that still produces no results.

## What Replaced It

See [sprint_2_2_real_discovery.md](../sprint_2_2_real_discovery.md) for the redesigned scope, which focuses on:

- Expanding fixture datasets from ~40 to 500+ entries
- Building reusable crawler infrastructure (`app/crawlers/`)
- Implementing industry keyword expansion
- Unifying the dual SDK into one canonical framework
- Proving end-to-end that "Roofing Texas" returns results

## Supporting Documents

- [Source Validation Report](../sprints/source_validation_report.md) — Why no free live sources exist
- [Discovery Architecture](../../architecture/discovery_architecture.md) — Permanent blueprint
- [ADR-001](../../decisions/ADR-001-Connector-Architecture.md) — Why connector architecture was chosen
- [ADR-002](../../decisions/ADR-002-No-Production-Fixtures.md) — Policy on fixture usage
