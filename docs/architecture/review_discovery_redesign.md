# LeadHunter Pro — Discovery Engine Architecture Review & Redesign

**Date:** 2026-08-03  
**Type:** Engineering Design Document  
**Scope:** Complete architectural assessment of the company discovery pipeline  
**Status:** DRAFT — awaiting review

---

## 1. Current Architecture

### 1.1 Component Map

The repository contains **two parallel connector architectures** that were built in different sprints and are currently wired together via adapter classes:

```
                         ┌──────────────────────────────────────────────────┐
                         │                 API Layer                        │
                         │                                                  │
                         │  GET /api/v1/discovery/companies  (discovery.py) │
                         │  GET /api/v1/connectors/texas-procurement        │
                         └─────────────────────┬────────────────────────────┘
                                               │
                    ┌───────────────────────────┼───────────────────────────┐
                    │                           │                           │
                    ▼                           ▼                           ▼
         ┌─────────────────────┐    ┌──────────────────────┐    ┌─────────────────────┐
         │ CompanyDiscoveryEn │    │  ConnectorManager     │    │  TexasProcurement   │
         │  (engines/)        │    │  (connectors/)        │    │  Adapter            │
         │                     │    │                       │    │  (connectors/)      │
         │ discover()          │───►│ discover()            │───►│ search()             │
         │  ↓                  │    │  ↓                    │    │  ↓                   │
         │ clean_companies()   │    │ validate_results()    │    │ self._legacy.        │
         │  ↓                  │    │  ↓                    │    │  discover()          │
         │ validate_companies()│    │ _rank_results()       │    │  ↓                   │
         │  ↓                  │    │  ↓                    │    │ _verify_url()        │
         │ return results      │    │ return ranked[:limit] │    │  ↓                   │
         └─────────────────────┘    └──────────────────────┘    │ ConnectorResult[]    │
                                                                └────────┬────────────┘
                                                                         │
                                                              ┌──────────┴──────────┐
                                                              │ Legacy Connector     │
                                                              │ (engines/source_)     │
                                                              │ connectors/)          │
                                                              │                        │
                                                              │ discover()              │
                                                              │  ↓                       │
                                                              │ Filter _TAXAS_… list    │
                                                              │ Return raw dicts        │
                                                              └─────────────────────────┘
```

### 1.2 Two SDK Systems in Parallel

| Dimension | `app/connectors/` (New) | `app/engines/source_connectors/` (SDK) |
|---|---|---|
| **Base class** | `BaseConnector` (abstract) | `ConstructionSourceConnector` (abstract) |
| **Result type** | `ConnectorResult` | `CompanyResult` |
| **Method name** | `search()` | `discover()` |
| **Parameters** | `(industry, location, limit)` | `(state, city, industry, limit)` |
| **Registry** | `ConnectorRegistry` (priority-based) | `ConnectorRegistry` (separate singleton) |
| **Manager** | `ConnectorManager` (dedup+rank) | `ConnectorManager` (dedup only) |
| **Status** | Active — imported by discovery engine | Legacy — wrapped by adapters |
| **Tests** | `tests/connectors/test_*.py` | `tests/discovery/test_*.py` |

Both systems define their own `ConnectorRegistry`, `ConnectorManager`, and base classes. They are **not interoperable**. The only bridge is `app/connectors/adapters.py`.

### 1.3 Data Flow for a Search Query

Given `GET /api/v1/discovery/companies?industry=Roofing&location=Dallas Texas`:

1. `CompanyDiscoveryEngine.discover(industry="Roofing", location="Dallas Texas")`
2. Calls `ConnectorManager.discover(...)` which iterates over `ConnectorRegistry.get_enabled()`
3. Adapters (`TexasProcurementAdapter`, `AgcTexasAdapter`) call their legacy `_legacy.discover(state="TX", city="Dallas", industry="Roofing", limit=100)`
4. `TexasProcurementConnector.discover()` filters `_TAXAS_CONSTRUCTION_COMPANIES` list (~40 entries)
5. Industry filter `any(k in industry_focus.lower() or k in company_name.lower() for k in keywords)` — **keywords extracted from "roofing" = {"roofing"}**
6. None of the 40 enterprises contain "roofing" in their `industry_focus` → **0 results**
7. Same failure for "Plumbing Texas" and "Electrical Texas"

---

## 2. Problems

### P1: The Dataset Is a Static Fixture, Not a Discovery Source

`_TAXAS_CONSTRUCTION_COMPANIES` is a hardcoded list of ~40 large enterprise construction firms. Every query against the Texas Procurement connector is bounded by this list. The system calls itself "discovery" but performs only **filtering**.

**Evidence:**
- `limit=20` → 3 companies, `limit=100` → still 3 (or fewer after dedup/validation)
- Searching "Roofing Texas" → `[]`
- Searching "Commercial Construction Texas" → ≤40 results max, regardless of `limit`

### P2: No Actual Web Crawling or Live Data Fetching

Neither the Texas Procurement nor AGC Texas connectors perform real-time discovery. AGC Texas attempts a live fetch but falls back to fixtures on any error. Neither connector crawls procurement portals, trade directories, or bid databases.

### P3: Brand-New Company Discovery Is Impossible

The system cannot discover companies that don't already exist in the fixture data. If a roofing contractor in Dallas opens tomorrow, the system will never find it through the current architecture.

### P4: Dead Domain Problem

The `adapters.py` verification layer makes HTTP HEAD requests against every result's `source_url` and `website`. Several URLs in the fixture dataset are outdated or redirect to non-commercial pages. Results with dead websites are filtered out in `company_validator.py` (`_is_live_website`), compounding the result starvation.

### P5: Duplicate Website Domains Across Entries

The fixture data contains overlapping domains (e.g., Brasfield & Gorrie appears twice with different cities). The deduplicator collapses these, reducing yield further.

### P6: Industry Matching Is Too Narrow

Keyword matching on `industry_focus` requires the search term to literally appear in the field. "Roofing" won't match "Commercial construction, general contracting" even though a general contractor may do roofing work. The industry group mapping in `ConnectorManager._INDUSTRY_GROUPS` exists but is only used in `_matches_industry()`, which is called on the *result side* — not as a broadening strategy during discovery.

### P7: Dual Connector SDKs Create Confusion and Maintenance Burden

Two independent `BaseConnector` ABCs, two registries, two manager classes, and adapter glue code constitute an architectural contradiction. New connector development must target both interfaces or go through adapters. Tests exist for both systems. This violates the single-responsibility principle.

### P8: Deleted Test File

`backend/tests/discovery/test_company_discovery.py` was deleted (git status shows `D`). Tests for `CompanyDiscoveryEngine` are missing.

---

## 3. Root Cause Analysis

### The Fundamental Misalignment

**LeadHunter Pro's stated vision:** *"Discovers companies from public sources"* — an active discovery system that finds new companies.

**What the current implementation does:** Filters a static list of 40 pre-seeded companies through keyword matching, then validates the survivors.

The root cause is that **Sprint 2.1 (Company Discovery Engine) attempted web search-based discovery** using providers (Google, Bing, DuckDuckGo, Serper, SerpAPI) that are blocked or rate-limited. When those proved unreliable, Sprint 2.1R/2.2 pivoted to the connector architecture — but the first concrete connector implementation (Texas Procurement) defaulted to a **fixture-based fallback** rather than implementing real scraping, because:

1. The sprint scope didn't include building a crawler/pipeline layer
2. Real scraping was deferred with the note "In production this would be populated by scraping..."
3. No actual scraping infrastructure (HTML parsing, rate limiting, rotation) was implemented

The result is a system that looks like discovery but behaves like a lookup table.

### Why "Roofing Texas" Returns []

The keyword "roofing" is extracted from the industry string. The system searches for "roofing" inside each company's `industry_focus` field. None of the 40 enterprise contractors list "roofing" — they list things like "Commercial construction, general contracting." The match fails. Zero results. The company validator then has nothing to validate.

This is not a bug in the filtering logic — **it is a correct consequence of a fundamentally wrong data source.** Even if the matching were perfect, 40 companies is insufficient for a discovery engine targeting a state as large as Texas.

---

## 4. Required Redesign

### 4.1 Architecture Goal

Transform the system from **lookup table + filter** to **multi-source discovery pipeline**:

```
User Search (industry + location)
    ↓
Connector Manager (fan-out)
    ↓
┌─────────────────────────────────────────────────────────────┐
│  Each Connector:                                           │
│  1. Construct query/scope from (industry, location, limit) │
│  2. Fetch data from source (API, scrape, crawl)             │
│  3. Extract company records                                 │
│  4. Normalize to CompanyResult                              │
│  5. Return (results, metadata)                              │
└─────────────────────────────────────────────────────────────┘
    ↓
Normalization Layer (deduplicate across sources)
    ↓
Validation Layer (live website check, domain blocklist)
    ↓
Ranking Layer (score by relevance)
    ↓
Return to API
```

### 4.2 Unify the Two Connector SDKs

**Decision: Consolidate on `app/connectors/` as the canonical connector framework.**

Reasoning:
- `app/connectors/` is what the discovery engine actually calls
- It has priority-based execution, proper deduplication, and ranking
- The adapters are working (tests pass)
- `app/engines/source_connectors/` is the older SDK with separate registries and types that serve no active path

**Actions:**
- Deprecate `ConstructionSourceConnector` and `CompanyResult` from `sdk.py`
- Migrate Texas Procurement and AGC Texas to implement `BaseConnector` directly (eliminating adapter indirection)
- Keep the SDK utilities (`HTTPClient`, `RateLimiter`, `Deduplicator`, `Normalizer`, `ErrorHandler`) — these are well-designed and reused
- Remove the dual `ConnectorRegistry` classes; consolidate into one

### 4.3 Replace Fixtures With Real Data Sources

Each connector must fetch live data. The Texas Procurement connector should:

1. **Scrape Texas procurement portals** — TxDOT vendor lists, state contract databases, county bid portals
2. **Use the NCMA/AGC directories** — the AGC Texas connector already attempts this; make it the primary data source
3. **Add trade directory connectors** — Angie's List, HomeAdvisor, regional contractor associations
4. **Support API-based sources** — many procurement portals offer JSON APIs

For development/testing, maintain small fixture datasets but gate them behind an `ENABLE_FIXTURE_FALLBACK` flag. Production must not serve fixture-only results.

### 4.4 Implement a Crawler/Parsing Layer

The current architecture has no HTML parsing capability in the active path. The `agc_texas/parser.py` exists but is narrowly scoped. A reusable crawling layer should be added to `app/research/website/` or a new `app/crawlers/` module:

```
app/crawlers/
├── base.py           # CrawlRequest, CrawlResponse, BaseCrawler ABC
├── http_crawler.py   # Session-managed HTTP crawler with rate limiting
├── html_parser.py    # BeautifulSoup-based extraction utilities
└── robots.py         # robots.txt compliance checker
```

Connectors use the crawler to fetch pages, then apply source-specific parsers to extract company records.

### 4.5 Broaden Industry Matching

The current industry filter is too strict. It should use **expansion rules**:

```python
INDUSTRY_EXPANSION = {
    "roofing": ["roofing", "roof", "shingle", "gutter", "roof repair"],
    "plumbing": ["plumbing", "plumber", "pipe", "hvac", "heating", "cooling"],
    "electrical": ["electrical", "electrician", "wiring", "electrical contractor"],
    "general_contractor": ["general contractor", "gc", "contracting", "construction"],
    ...
}
```

When a user searches "Roofing", the connector expands to search for any of the related terms across `industry_focus`, `company_name`, and additional scraped fields (services offered, about page text).

### 4.6 Increase Dataset Volume

The target must be **hundreds to thousands of companies per source**, not 40. The expansion strategy:

| Connector | Target Sources | Est. Yield |
|---|---|---|
| Texas Procurement | TxDOT vendors, county bids, SageBill | 2,000–5,000 |
| AGC Texas | Member directory | 300–600 |
| (Future) National | NAICS directory, state registries | 10,000+ |
| (Future) Trade dirs | Angi, HomeAdvisor scrapes | 5,000+ |

With multiple connectors producing 500–2,000 results each, a `limit=100` request can genuinely return 100 companies after deduplication.

### 4.7 Fix the Validation Bottleneck

The `company_validator.py` HTTP HEAD check is a necessary quality gate but is currently applied synchronously to every result, slowing the pipeline. Consider:

- Making validation **async/concurrent** (use `aiohttp` or ThreadPoolExecutor)
- Adding a **cache** for known-dead domains so repeated queries don't re-check
- Treating validation failures as **warnings**, not hard rejections — mark the company as "unverified" rather than dropping it entirely

---

## 5. Migration Strategy

### Phase 1: Consolidate the SDK (No Behavior Change)

**Goal:** Eliminate the dual-registry confusion without changing external behavior.

1. Move `ConstructionSourceConnector` → merge into `BaseConnector` (add `discover()` as an alias or keep both)
2. Keep both `CompanyResult` and `ConnectorResult` temporarily — add conversion helpers
3. Update `TexasProcurementAdapter` and `AgcTexasAdapter` to call the legacy connectors (already works)
4. Add deprecation warnings on `app/engines/source_connectors/sdk.py` types
5. **Tests:** All existing tests should continue to pass. No API behavior changes.

### Phase 2: Replace Texas Procurement Fixtures With Live Scraping

**Goal:** Texas Procurement returns real companies, not a hardcoded list.

1. Implement `TexasProcurementConnector` to scrape TxDOT vendor lookup API
2. Add parser for the portal's HTML/API response format
3. Store fetched results in a local cache (SQLite or JSON file) with TTL
4. Add integration tests that verify the scraper runs without errors (mocked HTTP responses)
5. **Tests:** New integration tests for the scraper; old unit tests updated to mock HTTP responses instead of fixtures

### Phase 3: Implement the Crawler Layer

**Goal:** Reusable HTTP crawling infrastructure for all future connectors.

1. Build `app/crawlers/http_crawler.py` with session management, rate limiting, retry
2. Build `app/crawlers/html_parser.py` with common extraction helpers
3. Update AGC Texas connector to use the crawler
4. **Tests:** Unit tests for the crawler (mocked HTTP), parser (sample HTML)

### Phase 4: Broaden Discovery Scope

**Goal:** Support "Roofing Texas", "Plumbing Texas", etc. returning meaningful results.

1. Add `INDUSTRY_EXPANSION` mapping
2. Expand industry matching in both the connector filter and the post-discovery ranker
3. Add 2–3 new connectors (trade directories, state registries)
4. Ensure `limit=100` can return 100 companies from the combined pool

### Phase 5: Optimization and Resilience

**Goal:** Production-grade reliability.

1. Async validation with domain cache
2. Connector health monitoring and automatic failover
3. Result persistence (store discovered companies in PostgreSQL for reuse)
4. Rate limiting coordination across connectors

---

## 6. Risk Analysis

| Risk | Severity | Mitigation |
|---|---|---|
| Scraping targets employ anti-bot measures (CAPTCHAs, WAFs) | High | Use official APIs where available; respect robots.txt; add exponential backoff; consider third-party data providers as fallback |
| Fixture-dependent tests break when live scraping is enabled | Medium | Keep `ENABLE_FIXTURE_FALLBACK` flag; run fixture tests in CI with flag set; run live tests only on demand |
| Consolidating two SDKs breaks integration tests | Medium | Run full test suite after each phase; use adapters as backward-compat layer during transition |
| Dead domains increase as companies change websites | Medium | Async validation with caching; treat as warning, not rejection; surface unverified companies separately |
| Industry expansion introduces false positives | Low | Start with conservative expansion lists; allow users to tighten/loosen with a `strict_mode` flag |
| TxDOT/procurement portal structure changes break scraper | Medium | Write parser tests against saved HTML snapshots; add regression detection |
| Two-registry system causes subtle bugs (wrong registry queried) | High | Phase 1 consolidation eliminates this entirely |

---

## 7. Backward Compatibility

### API Compatibility

The existing API endpoints are preserved:
- `GET /api/v1/discovery/companies?industry=&location=&limit=` — unchanged signature
- `GET /api/v1/connectors/texas-procurement?state=&city=&industry=&limit=` — unchanged signature

### Data Compatibility

`CompanyDiscoveryResult` (used by the API response) retains its current shape. The conversion layer in `CompanyDiscoveryEngine.discover()` adapts from `ConnectorResult` → `CompanyDiscoveryResult`. This layer remains unchanged during the migration.

### Test Compatibility

- All existing `tests/connectors/test_*.py` tests continue to pass
- All existing `tests/discovery/test_*.py` tests continue to pass
- New tests are added for scraping behavior; old fixture-based tests are marked with `@pytest.mark.integration`

---

## 8. Implementation Plan

### Week 1 — Foundation

| Task | Files | Effort |
|---|---|---|
| Merge dual SDKs: add `discover()` alias to `BaseConnector`, add `CompanyResult ↔ ConnectorResult` converters | `sdk.py`, `base.py`, `connector_result.py` | Medium |
| Migrate adapters to direct inheritance (remove adapter indirection) | `adapters.py`, `texas_procurement.py` | Medium |
| Deprecate `ConstructionSourceConnector` with warnings | `base.py` (engines) | Small |
| Add deprecation warnings to legacy SDK types | `sdk.py` (engines) | Small |
| Verify all tests pass | All test files | Small |

### Week 2 — Scraping Infrastructure

| Task | Files | Effort |
|---|---|---|
| Create `app/crawlers/` package with HTTP crawler | `http_crawler.py`, `html_parser.py`, `robots.py` | Large |
| Add unit tests for crawler (mocked responses) | `tests/crawlers/` | Medium |
| Add unit tests for HTML parser (sample markup) | `tests/crawlers/` | Medium |
| Add rate limiting and retry to crawler | `http_crawler.py` | Medium |

### Week 3 — Texas Procurement Rewrite

| Task | Files | Effort |
|---|---|---|
| Implement TxDOT vendor API integration | `texas_procurement.py` | Large |
| Implement county bid portal scrapers | `texas_procurement.py` | Large |
| Add local result cache with TTL | `texas_procurement.py` | Medium |
| Add industry expansion mapping | `texas_procurement.py`, `company_normalizer.py` | Small |
| Update tests for live scraping | `tests/connectors/` | Medium |
| Generate fixture data from real API responses (for offline testing) | `tests/fixtures/` | Medium |

### Week 4 — AGC Texas Enhancement + New Connectors

| Task | Files | Effort |
|---|---|---|
| Refactor AGC Texas to use new crawler | `agc_texas/` | Medium |
| Add AGC Texas member directory as primary source | `agc_texas/connector.py` | Medium |
| Create example trade directory connector | `connectors/trade_directory.py` | Large |
| Add 2+ new connectors (state registries, trade dirs) | `connectors/` | Large |
| Integration tests for multi-connector discovery | `tests/connectors/` | Medium |

### Week 5 — Pipeline Hardening

| Task | Files | Effort |
|---|---|---|
| Async validation with domain cache | `company_validator.py` | Medium |
| Improve ranking with expanded industry signals | `connector_manager.py` | Medium |
| Add `strict_mode` flag for industry matching | API schema, engine | Small |
| Full end-to-end test: "Roofing Texas" → valid results | Integration tests | Medium |
| Documentation update | `connector_sdk.md`, README | Small |

---

## 9. Estimated Sprint Breakdown

### Recommendation: This Becomes **Sprint 2.3** (Not Sprint 2.2)

**Why not Sprint 2.2?**

Sprint 2.2 (`docs/sprints/2.2_connector_sdk.md`) has a well-defined scope:
- Finalize SDK interfaces
- Expand Texas Procurement **fixtures** (add missing cities)
- Create example connector template
- Integration tests for registered connectors
- API route coverage
- Documentation updates

These tasks are about **polishing the connector framework**, not about fixing the fundamental discovery problem. Sprint 2.2 can proceed as documented — it delivers a solid SDK foundation.

The redesign described in this document requires:
- **Real web scraping infrastructure** (new module: `app/crawlers/`)
- **Live data source integration** (TxDOT API, county portals)
- **Multiple new connectors** (trade directories, state registries)
- **Industry expansion logic**
- **Pipeline optimization** (async validation, caching)

This is materially larger and different from Sprint 2.2's scope. It deserves its own sprint.

### Proposed Sprint 2.3 — Real Discovery Pipeline

| Component | Sprint |
|---|---|
| SDK consolidation (dual-registry cleanup) | Sprint 2.2 (finish first) |
| Crawler layer (`app/crawlers/`) | Sprint 2.3 |
| Texas Procurement live scraping | Sprint 2.3 |
| Industry expansion + broader matching | Sprint 2.3 |
| 2+ new connectors (trade directories) | Sprint 2.3–2.4 |
| Async validation + domain cache | Sprint 2.4 |
| End-to-end testing + documentation | Sprint 2.4 |

### Minimum Viable Redesign (If Sprint 2.3 Must Be Tight)

If resources are constrained, the minimum viable change to fix the reported QA failures is:

1. **Expand `_TAXAS_CONSTRUCTION_COMPANIES`** from ~40 to ~200 entries covering roofing, plumbing, electrical, HVAC, painting, flooring, etc. — This fixes the immediate "returns []" problem **without** implementing scraping.
2. **Relax industry matching** — use broader keyword matching (substring, not exact) so "roofing" matches "roofing contractor" and vice versa.
3. **Remove or relax the HTTP HEAD validation** — stop killing results that happen to have a temporarily unreachable website.

This is a **bridge solution**, not the real fix. It addresses the symptoms but not the root cause (static dataset). The full redesign above is required for a genuine discovery engine.

---

## 10. Specific Answers to the Eight Questions

### Q1: Which components are still dependent on fixture data?

| File | Dependency | Reason |
|---|---|---|
| `app/engines/source_connectors/texas_procurement.py` | `_TAXAS_CONSTRUCTION_COMPANIES` (line 27) | Hardcoded list of ~40 dicts, no live fetching |
| `app/engines/source_connectors/agc_texas/connector.py` | `_FIXTURE_PATH` (line 29) | Falls back to fixture JSON when live fetch fails |
| `app/engines/source_connectors/example_connector.py` | `_FIXTURE_PATH` | Reads from local JSON file |
| `backend/app/connectors/adapters.py` | Indirect via Texas/AGC connectors | Adapters wrap the legacy connectors which use fixtures |
| `backend/app/connectors/connector_manager.py` | Indirect | Delegates to adapters which delegate to fixture-based connectors |

**The discovery engine itself (`company_discovery_engine.py`) does NOT depend on fixtures.** It depends on `ConnectorManager`, which delegates to connectors. The fixture dependency flows from the bottom up.

### Q2: Which connectors actually discover companies? Which only filter?

| Connector | Type | Verdict |
|---|---|---|
| `TexasProcurementConnector` | Filters `_TAXAS_CONSTRUCTION_COMPANIES` | **Filter only** — static list, zero live fetching |
| `AgcTexasConnector` | Attempts live fetch → falls back to fixture | **Hybrid** — tries discovery but silently defaults to filtering when live access fails |
| `MockConnector` | Returns `_MOCK_COMPANIES` | **Filter only** — test fixture |
| `ExampleConnector` | Reads from JSON fixture | **Filter only** — test/dev fixture |

**No connector in the current codebase performs real discovery.** Every active production path traces back to a static dataset.

### Q3: What must change so "Roofing Texas" returns roofing companies?

**Three changes, in order of impact:**

1. **Expand the dataset.** Add hundreds of roofing, plumbing, electrical, and other specialty contractor records to the Texas dataset. The current 40 entries are all large general contractors — none specialize in trades.

2. **Broaden industry matching.** Change the keyword filter from exact-substring-in-`industry_focus` to a multi-field search: `company_name`, `industry_focus`, `description` (if available), plus an industry expansion table that maps "roofing" → ["roof", "shingle", "gutter", "tile"].

3. **Add real data sources.** Scrape contractor license databases (e.g., Texas LPB contractor lookup) and trade association directories. A single scraper against the Texas Licensing Board database would return thousands of roofing contractors.

### Q4: What must change so `limit=100` can return 100 companies?

**The limit is already correctly applied** — `ranked[:limit]` truncates at the requested count. The problem is upstream: there are never 100 candidates to begin with.

To make `limit=100` return 100 companies:
1. Each connector must produce ≥100 raw results before deduplication
2. Multiple connectors must run in parallel/fan-out to aggregate diverse sources
3. Deduplication must not eliminate more than ~30% of results (currently it may eliminate more due to overlapping domains in the fixture data)
4. Validation must not reject too many results (currently ~20–40% fail the HEAD check due to stale URLs)

**Concrete requirement:** Each active connector needs a database of **500+ companies** minimum, and at least **3 connectors** should be healthy for a `limit=100` request to reliably return 100 results.

### Q5: How should connectors behave?

**Correct behavior (target):**
```
1. Parse user query → (industry, city, state, limit)
2. Query source API or crawl source website
3. Extract structured company records from response
4. Normalize fields (name, url, location, industry)
5. Return (list[CompanyResult], metadata with source info)
```

**Current broken behavior:**
```
1. Receive user query
2. Filter hardcoded list
3. Return filtered subset
```

The difference: **step 2 must fetch, not filter.** The connector should call an external API or scrape a website to get fresh data. The fixture data should only exist as a test aid, never as the sole data source in production.

### Q6: What discovery pipeline should exist?

```
GET /api/v1/discovery/companies?industry=&location=&limit=
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│  CompanyDiscoveryEngine.discover()                          │
│  1. Parse location → (city, state)                          │
│  2. Fan-out to all enabled connectors                       │
│     (parallel or sequential with timeout)                   │
└───────────────────────────┬─────────────────────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
   ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
   │ Texas Proc.  │ │ AGC Texas    │ │ Trade Dir.   │
   │ (scrapes     │ │ (scrapes     │ │ (scrapes     │
   │  TxDOT,      │ │  AGC dir)    │ │  Angie's,    │
   │  county bids)│ │              │ │  HomeAdvisor)│
   └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
          │                │                │
          └────────────────┼────────────────┘
                           ▼
              ┌────────────────────────┐
              │  Normalizer            │
              │  (unify field names)   │
              └───────────┬────────────┘
                           ▼
              ┌────────────────────────┐
              │  Deduplicator          │
              │  (by domain + name)    │
              └───────────┬────────────┘
                           ▼
              ┌────────────────────────┐
              │  Ranker                │
              │  (state + city +       │
              │   industry match)      │
              └───────────┬────────────┘
                           ▼
              ┌────────────────────────┐
              │  Validator             │
              │  (live website check)  │
              │  (async, cached)       │
              └───────────┬────────────┘
                           ▼
              ┌────────────────────────┐
              │  Truncate to limit     │
              │  Return results        │
              └────────────────────────┘
```

### Q7: Which modules should remain exactly as they are? Which need redesign?

**Remain unchanged:**
| Module | Reason |
|---|---|
| `api/v1/discovery.py` | Clean FastAPI route; passes correct params to engine |
| `api/v1/router.py` | Router aggregation — no logic to change |
| `engines/discovery/company/company_models.py` | Data classes are fine; remove deprecated `SUPPORTED_SOURCES` literal values referencing dead search providers |
| `engines/discovery/company/company_cleaner.py` | Dedup+normalize logic is sound; already correct |
| `engines/discovery/company/company_validator.py` | Validation logic is sound; needs async+cache optimization but not redesign |
| `connectors/connector_registry.py` | Well-designed priority-based registry |
| `connectors/connector_result.py` | Clean frozen dataclass |
| `connectors/company_normalizer.py` | Good utility functions |
| `engines/source_connectors/deduplicator.py` | Good utility, just needs to move to canonical location |
| `engines/source_connectors/error_handler.py` | Good utility |
| `engines/source_connectors/connector_logger.py` | Good utility |
| `engines/source_connectors/http_client.py` | Good utility |
| `engines/source_connectors/rate_limiter.py` | Good utility |
| `engines/source_connectors/retry_manager.py` | Good utility |

**Need redesign:**
| Module | Issue | Action |
|---|---|---|
| `engines/source_connectors/texas_procurement.py` | Uses static fixtures, no live scraping | Rewrite to scrape TxDOT/vendor portals |
| `engines/source_connectors/agc_texas/connector.py` | Silently falls back to fixtures | Make live fetch mandatory; use fixtures only in tests |
| `connectors/adapters.py` | Unnecessary indirection layer | Eliminate — connectors should implement `BaseConnector` directly |
| `engines/discovery/company/company_discovery_engine.py` | Converts between two result types | Simplify once SDKs are unified |
| `engines/discovery/company/company_models.py` | `SUPPORTED_SOURCES` references dead search providers | Clean up literal values |
| `engines/source_connectors/sdk.py` | Duplicate registry and base class | Deprecate; migrate to `connectors/` equivalents |
| `api/v1/connectors.py` | Bypasses the pipeline, calls connector directly | Route through `ConnectorManager` for consistency |

### Q8: Can this redesign happen without breaking ConnectorManager, DiscoveryEngine, API, Swagger, Tests?

**Yes, with a phased approach.**

**Phase 1 (no behavioral change):**
- Consolidate the two registries and base classes
- Keep adapters in place during transition
- All existing tests pass
- API contracts unchanged
- Swagger UI unchanged (same endpoints, same request/response shapes)

**Phase 2+ (behavioral improvement, same interface):**
- Replace Texas Procurement's internal `discover()` implementation
- The method signature stays the same; the implementation changes from filtering to scraping
- `ConnectorManager.discover()` doesn't change — it already calls `connector.search()` (adapter) → `connector.discover()` (legacy)
- API response shape stays the same
- Existing tests need updating (fixtures → mocked HTTP), but the test structure remains valid

**The only breaking change is the deletion of `app/engines/source_connectors/sdk.py`'s dual types** — this requires updating imports in adapters and tests. This is an internal refactor, not an API change.

**Swagger/OpenAPI:** The API schema is defined by Pydantic models (`CompanyDiscoveryResult`) and FastAPI decorators. Neither changes during this redesign. Swagger continues to work without modification.

---

## 11. Recommendation

### This Should Become **Sprint 2.3** — "Real Discovery Pipeline"

**Sprint 2.2 should complete as documented** (SDK finalization, fixture expansion, example connector, tests, docs). This delivers the framework.

**Sprint 2.3** should then implement the actual discovery capabilities:
- Crawler layer
- Live Texas Procurement scraping
- Industry expansion
- Additional connectors
- Pipeline hardening

**Why not fold it into Sprint 2.2?** Sprint 2.2's acceptance criteria are achievable in a standard sprint window. Adding live scraping, a new crawler module, and multiple new connectors would bloat the scope beyond a single sprint's capacity and risk delivering neither the polish Sprint 2.2 promises nor the depth Sprint 2.3 requires.

**Why not call it Sprint 2.1R Revision?** Sprint 2.1R was a reactive fix (replace broken search providers with connector stubs). This redesign is a proactive architectural upgrade. It's a new capability, not a correction of a previous failure. Sprint 2.3 reflects this better.

---

## Appendix A: Files That Must Be Created

| New File | Purpose |
|---|---|
| `app/crawlers/__init__.py` | Crawler package init |
| `app/crawlers/base.py` | `BaseCrawler` ABC, `CrawlRequest`, `CrawlResponse` |
| `app/crawlers/http_crawler.py` | Session-managed HTTP crawler with retries |
| `app/crawlers/html_parser.py` | BeautifulSoup helper for company extraction |
| `app/crawlers/robots.py` | robots.txt compliance |
| `tests/crawlers/test_http_crawler.py` | Crawler unit tests |
| `tests/crawlers/test_html_parser.py` | Parser unit tests |
| `tests/fixtures/txdot_vendor_sample.json` | Sample TxDOT API response for testing |

## Appendix B: Files That Must Be Deleted

| File | Reason |
|---|---|
| `backend/tests/discovery/test_company_discovery.py` | Already deleted; no replacement yet needed until SDK is unified |

## Appendix C: Files That Must Be Modified

| File | Change |
|---|---|
| `app/connectors/adapters.py` | Remove adapter indirection; have connectors implement `BaseConnector` directly |
| `app/engines/source_connectors/sdk.py` | Add deprecation warnings to `ConstructionSourceConnector`, `CompanyResult`, legacy `ConnectorRegistry` |
| `app/engines/source_connectors/texas_procurement.py` | Replace fixture list with live scraper; add industry expansion |
| `app/engines/source_connectors/agc_texas/connector.py` | Remove fixture fallback; use crawler |
| `app/engines/discovery/company/company_models.py` | Remove dead provider names from `SUPPORTED_SOURCES` |
| `app/api/v1/discovery.py` | Update docstring (remove reference to search providers/CAPTCHA) |
| `app/api/v1/connectors.py` | Route through `ConnectorManager` instead of calling connector directly |
| `docs/sprints/2.2_connector_sdk.md` | Mark Sprint 2.2 as the SDK foundation sprint; create Sprint 2.3 doc |
| `CLAUDE.md` | Update sprint status to reflect 2.3 as next sprint |
