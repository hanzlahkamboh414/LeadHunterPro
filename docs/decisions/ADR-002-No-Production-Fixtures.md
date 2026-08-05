# ADR-002: No Production Fixtures

**Status:** Accepted  
**Date:** 2026-08-03  
**Decided by:** Hanzlah (Founder) & Agnes (AI Engineering)  
**Supersedes:** None  
**Related:** [docs/architecture/discovery_architecture.md](../architecture/discovery_architecture.md)

---

## Context

During Sprint 2.1R and the Sprint 2.2 redesign process, a fundamental question emerged: **Should discovery connectors use live data sources or static fixture data?**

Manual QA revealed that the current Texas Procurement connector returns at most ~40 companies from a hardcoded list. This is not discovery — it is filtering. The system's stated purpose is to discover companies from public sources, yet no connector performs actual discovery.

This ADR establishes the permanent engineering rule governing when and where static data may be used in production discovery paths.

---

## Problem

The distinction between "discovery" and "filtering" is critical to LeadHunter Pro's value proposition:

| Behavior | Definition | Example |
|---|---|---|
| **Discovery** | Actively fetching data from an external source | Scraping TxDOT vendor portal, calling a public API, crawling a trade directory |
| **Filtering** | Selecting from an existing static dataset | Searching a hardcoded list of 40 companies |

The current system performs filtering disguised as discovery. Users expect discovery but receive lookup-table results. This mismatch:
- Produces empty results for trade-specific queries ("Roofing Texas" → `[]`)
- Caps output at ~40 companies regardless of `limit`
- Returns identical results on every query
- Cannot find companies that weren't manually entered

The question is not whether fixtures are evil — they are essential for testing. The question is whether they are allowed in the **production discovery path**.

---

## Options Considered

### Option 1: Fixtures Forbidden in Production (Selected)

Connectors MUST fetch data from live sources in production. Fixtures are permitted only in test code. Any connector that cannot reach a live source returns an error, not empty fixture results.

**Pros:**
- True discovery behavior
- Results reflect current reality
- System cannot silently return stale/incorrect data
- Clear engineering standard

**Cons:**
- Many valuable sources are behind paywalls or WAFs (see Source Validation Report)
- No freely-scrapable high-volume sources exist today
- Development and CI would lose deterministic test data
- Hard to prototype without any fixture data

**Effort:** High — requires finding or building real data sources

---

### Option 2: Fixtures Allowed in Production (Current State — REJECTED)

Connectors may use fixtures as their primary or fallback data source. No enforcement mechanism.

**Pros:**
- Works immediately with zero external dependencies
- Deterministic results for testing
- Fast development iteration

**Cons:**
- Violates the product's core value proposition
- Results are bounded by manually curated data size
- No new companies can be discovered
- Silent degradation — system appears to work but returns stale data
- Technical debt accumulates; fixing it later is harder

**Effort:** Low — but creates unsustainable debt

---

### Option 3: Fixtures as Bridge, Live Sources as Goal

Fixtures are allowed as a transitional mechanism while live sources are being secured. Once a live source is available for a connector, fixtures must be disabled in production. An environment flag controls this behavior.

**Pros:**
- Allows immediate progress while sourcing partnerships mature
- Clear migration path to live sources
- Environment flag provides explicit control
- Testing remains deterministic with fixtures

**Cons:**
- Temporary state persists indefinitely without enforcement
- Risk of forgetting to flip the flag
- Creates confusion about what "production" means

**Effort:** Medium — requires flag management and migration tracking

---

## Decision

**Selected Option: Option 1 — Fixtures Forbidden in Production**, with a pragmatic exception for the current sprint cycle.

### The Rule

```
Production discovery MUST NEVER depend on static fixtures.
Fixtures are allowed ONLY for:
  - Unit testing
  - Integration testing
  - CI/CD pipelines
```

This rule is enforced by:
1. **Code convention:** Connectors document their data source in metadata (`data_source: "live"` vs `data_source: "fixture"`)
2. **Environment check:** Production deployments set `ENVIRONMENT=production`; any connector reporting `data_source: "fixture"` triggers a startup warning
3. **Test enforcement:** Integration tests mock HTTP responses instead of using real fixtures for production-path verification

### Pragmatic Exception for Sprint 2.2

Because no legally-scrapable live sources exist today (see Source Validation Report), Sprint 2.2 uses **curated fixture datasets** as a bridge:

- Fixtures contain 500+ entries compiled from public records (county bid awards, published contractor lists)
- Each fixture entry includes `data_provenance` metadata documenting its source
- Metadata indicates `"data_source": "fixture"` so operators know the limitation
- Sprint 2.3 targets at least one live Tier 2 source (county bid portal or TLGB)
- The bridge is time-boxed: Sprint 2.2 → Sprint 2.3 handoff requirement

### What This Means for Connectors

| Scenario | Required Action |
|---|---|
| Live API available | Implement against API; no fixtures |
| Public website scrapable | Implement scraper; no fixtures |
| No live source exists yet | Use curated fixtures WITH provenance metadata; mark as bridge |
| Fixture-only in tests | Standard test practice; no restriction |
| Silent fixture fallback | **Forbidden** — must fail loudly if live source unavailable |

---

## Consequences

### Positive

- **Truthfulness:** Every company returned by the discovery engine represents real, verifiable data
- **Scalability:** As new data sources become available (partnerships, API access), they plug in without architectural changes
- **Diagnostic clarity:** Operators can always determine whether results came from live sources or fixtures via metadata
- **Engineering discipline:** Removes the temptation to ship "good enough" fixture-based results
- **User trust:** No silent degradation — users get real companies or clear error messages

### Negative

- **Initial gap:** There is a period (Sprint 2.2) where results come from curated fixtures because no live sources are available
- **Development friction:** Local development requires either fixture mode or mocked HTTP for all connectors
- **Migration effort:** Existing legacy connectors using silent fixture fallback must be updated to declare their data source explicitly

### Mitigations

- Curated fixtures include `data_provenance` field documenting where each entry originated
- Source Validation Report identifies which Tier 2 sources to target next
- Sprint 2.3 mandate: at least one connector must use a live source before Sprint 2.2 is marked complete
- Automated check in CI: warn if all active connectors report `data_source: "fixture"`

---

## Alternatives Rejected

| Option | Reason Rejected |
|---|---|
| Fixtures Allowed (current state) | Violates core product value proposition. Produces false sense of working discovery. |
| Fixtures as Bridge Only (no enforcement) | Temporary measures tend to become permanent. Without a hard rule, the bridge never ends. |
| Per-Connector Fixture Allowlist | Creates classification noise — why is Source A allowed fixtures but Source B isn't? Uniform rule is simpler and fairer. |

---

## Future Impact

### On Connector Development

Future connectors must either:
1. Connect to a live data source (API, scrape, database)
2. If no live source exists, use curated fixtures with full provenance documentation AND target a live source in the next sprint

### On Testing

All production-path tests must mock HTTP responses (using saved HTML snapshots or JSON fixtures in `tests/fixtures/`). Real HTTP calls are only made in designated integration tests run on demand.

### On Deployment

Deployment checklists will include: "Verify no production connector reports `data_source: fixture`" as a gate criterion.

### Follow-up Actions

- [ ] Add `data_source` field to `ConnectorResult.metadata`
- [ ] Add CI check that warns on all-fixture production runs
- [ ] In Sprint 2.3, implement at least one live Tier 2 source
- [ ] Document source acquisition plan in roadmap

### Review Date

Revisit when the first live-sourced connector is deployed to production, or after Sprint 2.3 completes.

---

## References

- [docs/sprints/source_validation_report.md](../sprints/source_validation_report.md) — Source feasibility analysis justifying the Sprint 2.2 bridge
- [docs/architecture/discovery_architecture.md](../architecture/discovery_architecture.md) — Connector lifecycle and data source requirements
- [docs/sprints/sprint_2_2_real_discovery.md](../sprints/sprint_2_2_real_discovery.md) — Sprint 2.2 scope including bridge strategy
