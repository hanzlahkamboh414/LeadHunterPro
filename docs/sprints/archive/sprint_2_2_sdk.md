# Sprint 2.2 — Connector SDK (Archived)

**Original Date:** Sprint 2.2  
**Archived:** 2026-08-03  
**Reason:** Superseded by `sprint_2_2_real_discovery.md` following Manual QA findings that revealed the discovery pipeline is fixture-dependent rather than genuinely discovering companies from public sources.

---

The original Sprint 2.2 focused on polishing the connector SDK framework (interfaces, test coverage, API routes, documentation) but did not address the core architectural failure: **all connectors return filtered fixture data instead of performing real discovery from public sources.**

See [../architecture/review_discovery_redesign.md](../../architecture/review_discovery_redesign.md) for the full engineering review that triggered this archival.
