# ADR-001: Connector Architecture for Company Discovery

**Status:** Accepted  
**Date:** 2026-08-03  
**Decided by:** Hanzlah (Founder) & Agnes (AI Engineering)  
**Supersedes:** None  
**Related:** [docs/architecture/discovery_architecture.md](../architecture/discovery_architecture.md)

---

## Context

LeadHunter Pro's core value proposition is discovering construction companies from public sources. Sprint 2.1 attempted to build this capability using Google/Bing/DuckDuckGo/Serper/SerpAPI search providers. These providers proved unreliable in production due to CAPTCHA blocks, rate limits, and inconsistent result quality.

The system needed a fundamentally different approach — one that:
- Does not depend on search engine availability
- Returns structured, domain-specific data (industry focus, revenue tier, trade category)
- Supports adding new data sources without modifying core engine code
- Provides deterministic, testable behavior

---

## Problem

The existing discovery pipeline had these failures:

1. **Search providers blocked:** Google, Bing, DuckDuckGo return CAPTCHAs or rate-limit automated queries. Serper and SerpAPI require paid subscriptions.
2. **Noisy results:** Search-engine snippets lack structured fields (industry, revenue, location accuracy).
3. **Fragile dependency chain:** One provider failing could break the entire discovery flow.
4. **No extensibility:** Adding a new source required modifying the engine, violating Open/Closed Principle.
5. **Fixture bottleneck:** The fallback to static fixture data produced at most ~40 results, all enterprise general contractors — zero trade-specific coverage.

A connector-based architecture was identified as the solution, but the question remained: what kind of connector architecture?

---

## Options Considered

### Option 1: Provider Chain (Search-Based)

Multiple search providers in a failover chain: Google → Bing → DuckDuckGo → Serper → SerpAPI. Each provider returns raw search results; the engine parses and cleans them.

**Pros:**
- Leverages existing search infrastructure
- Wide coverage (all industries, all locations globally)
- No data curation required

**Cons:**
- All providers are blocked or rate-limited in production
- Results are unstructured and noisy
- CAPTCHA/WAF protection makes reliability poor
- Violates CLAUDE.md rule: "Google, Bing, DuckDuckGo, Serper, SerpAPI are NOT considered primary discovery providers"
- No structured domain data (industry focus, revenue tier)
- High operational cost (API subscriptions)

**Effort:** Medium (fix existing, add fallbacks)

---

### Option 2: Monolithic Scraper

A single monolithic module that scrapes all sources internally. One big class handles TxDOT, AGC Texas, county portals, trade directories, etc.

**Pros:**
- Simple mental model
- Single point of control
- Easy to debug

**Cons:**
- Violates Single Responsibility Principle
- Adding a new source requires modifying the monolith
- Testing is difficult (all sources coupled)
- One failing source can break all others
- No clear extension point for future connectors
- Code becomes unmaintainable at scale

**Effort:** Low initially, High long-term maintenance

---

### Option 3: Pluggable Connector SDK (Selected)

Each data source is an independent connector implementing a shared `BaseConnector` interface. Connectors are registered in a registry. A `ConnectorManager` orchestrates execution across all enabled connectors. The `DiscoveryEngine` delegates entirely to the manager and knows nothing about individual sources.

**Pros:**
- New sources added by creating one file (Open/Closed Principle)
- Each connector is independently testable
- Failure isolation — one broken connector doesn't break others
- Clear separation of concerns (engine vs. source logic)
- Supports heterogeneous data sources (fixtures, APIs, scrapers, databases)
- Priority-based execution ordering
- Registry enables dynamic connector discovery at runtime
- Aligns with Clean Architecture and SOLID principles

**Cons:**
- More files and abstraction layers
- Initial setup complexity
- Requires discipline to maintain separation
- Adapter layer needed during transition from legacy SDK

**Effort:** Medium (foundation), Low per-new-connector

---

## Decision

**Selected Option: Option 3 — Pluggable Connector SDK**

Every data source is implemented as an independent connector class inheriting from `BaseConnector`. The architecture enforces:

```
DiscoveryEngine → ConnectorManager → [ConnectorRegistry]
                                         ├── TexasProcurementConnector
                                         ├── AgcTexasConnector
                                         └── FutureConnector (zero engine changes)
```

The `DiscoveryEngine`:
- Accepts `(industry, location, limit)` from the API
- Expands industry keywords
- Delegates to `ConnectorManager`
- Receives `list[ConnectorResult]` back
- Applies deduplication, validation, ranking
- Returns `list[CompanyDiscoveryResult]` to the API

The `ConnectorManager`:
- Loads all enabled connectors from registry
- Executes each connector's `search()` method
- Collects and aggregates results
- Tracks per-connector errors and metadata
- Never inspects connector internals

Each `Connector`:
- Implements `search(industry, location, limit)` returning `(results, metadata)`
- Implements `health_check()` returning `bool`
- Implements `validate_result(result)` returning `bool`
- Chooses its own data-fetching strategy (fixtures, API, scraper, database)
- Knows nothing about other connectors or the engine

---

## Consequences

### Positive

- **Extensibility:** Adding a new data source requires one new file + one registry registration. Zero engine changes.
- **Testability:** Each connector can be unit-tested in isolation with mocked HTTP responses or fixture data.
- **Fault isolation:** One connector failing (e.g., AGC Texas WAF returns 403) does not prevent Texas Procurement from returning results.
- **Priority ordering:** Connectors can be ranked by priority; high-confidence sources execute first.
- **Metadata richness:** Each connector returns its own metadata (source type, record count, filters applied), enabling diagnostic visibility.
- **Strategic alignment:** Matches LeadHunter Pro's vision of "discovering companies from public sources" through purpose-built connectors rather than generic search.
- **Team scalability:** Multiple developers can work on different connectors simultaneously without conflicts.

### Negative

- **Abstraction overhead:** More files and interfaces than a monolithic approach. Initial development is slower.
- **Transition cost:** Dual-SDK migration (legacy `ConstructionSourceConnector` → new `BaseConnector`) requires adapter code and careful testing.
- **Registry management:** Connectors must be explicitly registered; forgetting to register breaks discovery silently.
- **Interface rigidity:** All connectors must conform to the same signature; highly customized sources may need adapter wrappers.

### Mitigations

- Auto-registration on import (`from app.connectors.my_source import MyConnector` triggers `ConnectorRegistry.register()`)
- Health checks surface missing/broken connectors at startup
- Comprehensive contract tests verify all connectors implement the interface correctly

---

## Alternatives Rejected

| Option | Reason Rejected |
|---|---|
| Provider Chain (Search-Based) | Explicitly prohibited by CLAUDE.md — Google/Bing/DuckDuckGo/Serper/SerpAPI are not primary discovery providers. All failed in Sprint 2.1 QA. |
| Monolithic Scraper | Violates SOLID principles. Adding a 10th source would require modifying a 2,000-line class. Unmaintainable at scale. |
| Database-First (Pre-Seeded DB) | Would require manual data entry or ETL pipelines before any discovery works. Too slow for Sprint 2.2. Connectors with fixtures achieve the same result more flexibly. |
| Event-Driven Connector Bus | Over-engineered for current scale. A simple sequential fan-out is sufficient; event bus adds unnecessary complexity. |

---

## Future Impact

### Immediate (Sprint 2.2)
- Consolidate dual SDK into single `BaseConnector` framework
- Migrate Texas Procurement and AGC Texas to direct `BaseConnector` implementations
- Delete adapter indirection layer
- Add crawler infrastructure (`app/crawlers/`) for future live-source connectors

### Near-Term (Sprint 2.3+)
- Add Tier 2 sources (county bid portals, TLGB license database) as new connector files
- Each new source requires: one Python file + one registry line + tests
- Crawler infrastructure is reused, not rewritten

### Long-Term (Sprint 3.x)
- Support async parallel connector execution for latency reduction
- Add distributed connector registry (Redis) for multi-worker deployments
- Add connector health monitoring dashboard
- Support connector versioning and A/B testing

### Follow-up Actions

- [ ] Execute Sprint 2.2A (documentation foundation — current sprint)
- [ ] Execute Sprint 2.2B (implementation: crawler layer, expanded fixtures, unified SDK)
- [ ] Execute Sprint 2.3 (live source integration: county portals, TLGB)
- [ ] Review ADR after Sprint 2.3 to assess connector adoption patterns

### Review Date

6 months from acceptance, or when more than 5 connectors are registered.

---

## References

- [docs/architecture/discovery_architecture.md](../architecture/discovery_architecture.md) — Full discovery architecture
- [docs/sprints/source_validation_report.md](../sprints/source_validation_report.md) — Source feasibility analysis
- [docs/sprints/sprint_2_2_real_discovery.md](../sprints/sprint_2_2_real_discovery.md) — Sprint 2.2 scope
- [CLAUDE.md](../../CLAUDE.md) — Project rules and sprint status
