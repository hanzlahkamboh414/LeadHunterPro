# LeadHunterPro — Project Analysis Report

**Date:** 2026-08-05  
**Prepared by:** Senior Software Architect (Analysis Phase)  
**Status:** COMPLETE — Ready for review

---

## 1. Executive Summary

LeadHunterPro is an **AI-powered construction lead intelligence platform** designed to discover construction companies from public sources, enrich them with AI-generated insights, score their fit, and produce outreach-ready reports. The backend is a **Python FastAPI** service following Clean Architecture and SOLID principles.

### Current State
- **Sprint 1.1** (Foundation Infrastructure): ✅ Done
- **Sprint 1.2** (Architecture Freeze): ✅ Done
- **Sprint 2.1** (Company Discovery Engine): ✅ Done
- **Sprint 2.2** (Connector SDK): 🔄 In Progress (Plugin framework implemented, but live sources missing)
- **Sprint 2.3** (Real Discovery Pipeline): 📋 DRAFT — Pending approval (Critical fix needed)

### Core Problem
The discovery system **currently returns fixture data only**. The `_fetch_live()` method in `TexasProcurementConnector` is a placeholder returning `[]`. No live web source is queried. The "Execute" button performs an instant lookup of curated JSON fixtures, not real discovery.

### Architecture Quality
The architecture is **well-designed and principled** (Clean Architecture, dependency inversion, provider-agnostic discovery). The main issue is **execution** — the live pipeline components (crawlers, search providers, website extraction) exist but are not yet wired into a working end-to-end flow.

---

## 2. High-Level Architecture

### 2.1 Layer Dependencies (Top-Down)

```
api/v1/              →  engines/  →  connectors/  →  crawlers/
                         services/       (internal)     (internal)
                         repositories/
                         models/
                         ai/
```

**Rule:** A layer may only depend on layers below it. Nothing flows upward.

### 2.2 Major Subsystems & Responsibilities

| Subsystem | Location | Responsibility |
|-----------|----------|----------------|
| **API Layer** | `app/api/v1/` | HTTP routing, input validation, response formatting |
| **Services** | `app/services/` | Business logic orchestration (company, research, email, leadership, crawler) |
| **Discovery Engine** | `app/engines/discovery/company/` | Orchestrates full discovery pipeline: connectors → dedup → validate → rank |
| **Connector Framework** | `app/connectors/` | Pluggable data sources (Texas Procurement, AGC Texas, future: CompanyDirectory) |
| **Source Orchestrator** | `app/discovery/source_orchestrator.py` | Multi-source execution with priority, status contract, fixture fallback |
| **Search Providers** | `app/search_providers/` | Brave, SearXNG — optional infrastructure, never core architecture |
| **Crawlers** | `app/crawlers/` | HTTP crawler with retry, rate limiting, robots.txt, cache, HTML parser |
| **Plugin Framework** | `app/discovery/plugins/` | Extensible discovery plugins (company, leadership, email, etc.) |
| **Website Discovery** | `app/discovery/website/` | Extractors, evidence, confidence, URL normalization/filtering |
| **AI Module** | `app/ai/` | Provider gateway (OpenAI, Anthropic, Gemini, Ollama), scorer, summarizer |
| **Research Pipeline** | `app/research/website/` | Website crawling, parsing, AI analysis (downstream of discovery) |
| **Scoring** | `app/scoring/` | Company lead scoring heuristics + AI |
| **Repositories** | `app/repositories/` | Data access layer (SQLAlchemy) |
| **Models/Schemas** | `app/models/`, `app/schemas/` | ORM models & Pydantic schemas |

### 2.3 Key Architectural Decisions

1. **Provider-agnostic discovery** — Discovery Engine never knows where data comes from; it issues queries, connectors respond (CLAUDE.md §4)
2. **Search APIs are optional infrastructure** — Brave, SearXNG are optional sources; discovery quality comes from strategy, crawling, extraction, classification, validation, ranking — NOT from any single provider (CLAUDE.md §3, §8)
3. **Fixtures are TEMPORARY BRIDGE DATA ONLY** — Must NEVER become primary discovery source; always attempt live discovery first (CLAUDE.md §1, ADR-002)
4. **AI isolation** — AI module is a leaf node; engines/services/connectors call AI; AI never calls back into business logic
5. **Plugin system = future-proof** — Every future source (government registries, trade directories, website crawlers) implements `BaseDiscoveryPlugin` interface

---

## 3. Runtime Execution Flow

### 3.1 Complete Pipeline: User Query → CompanyResult

```
User: GET /api/v1/discovery/companies?industry=Roofing&location=Dallas+Texas&limit=100
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│  API Layer: app/api/v1/discovery.py                             │
│  discover_companies(industry, location, limit)                  │
│    → Calls CompanyDiscoveryEngine.discover()                    │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  Discovery Engine: app/engines/discovery/company/               │
│  CompanyDiscoveryEngine.discover()                              │
│                                                                 │
│  1. Parse location → (city, state)                              │
│  2. Expand industry keywords via IndustryExpansion              │
│  3. Delegate to ConnectorManager.discover()                     │
│  4. Convert ConnectorResult → CompanyDiscoveryResult            │
│  5. clean_companies() — deduplicate by domain + name           │
│  6. validate_companies() — LIVE HEAD check                     │
│  7. Return (validated list, DiscoveryMetrics)                   │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  Connector Manager: app/connectors/connector_manager.py         │
│  ConnectorManager.discover(industry, location, limit)           │
│                                                                 │
│  1. Load enabled connectors from ConnectorRegistry (by priority)│
│  2. For each connector:                                         │
│     a. connector.search(industry, location, limit)              │
│     b. connector.validate_result() for each result              │
│  3. Aggregate all_results                                       │
│  4. _deduplicate() — domain, name, fuzzy (Jaccard ≥ 0.7)       │
│  5. _rank_results() — state(+40), city(+30), industry(+25),    │
│                        trusted(+10), live(+5), revenue(+5)     │
│  6. Return ranked[:limit] + metadata                            │
└──────────────────────────┬──────────────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
     │  CONNECTOR   │ │  CONNECTOR   │ │  CONNECTOR   │
     │ TX Procurement│ │  AGC Texas   │ │ Future #3    │
     │              │ │              │ │              │
     │ Implements   │ │ Implements   │ │ Implements   │
     │ BaseConnector│ │ BaseConnector│ │ BaseConnector│
     └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
            │                │                │
            ▼                ▼                ▼
     ┌──────────────────────────────────────────────────────┐
     │           CONNECTOR-INTERNAL SOURCES (per connector)  │
     │                                                      │
     │  [Source] → [Crawler] → [Parser] → [AI Extractor]    │
     │    → [Normalizer] → [Validator] → ConnectorResult[]  │
     │                                                      │
     │  Texas Procurement:                                  │
     │    SourceOrchestrator → PluginSource/                │
     │    SearchProviderSource/FixtureSource                │
     │    → Industry expansion filter → Build results       │
     │                                                      │
     │  CompanyDirectory (Sprint 2.3):                      │
     │    HTTPCrawler → listing pages → company pages       │
     │    → extract name, website, city, trade              │
     └──────────────────────────────────────────────────────┘
```

### 3.2 Texas Procurement Connector Execution (Current)

```
TexasProcurementConnector.search():
    1. SourceOrchestrator discovers:
       a. PluginSource (if plugins registered) — NOOP currently
       b. SearchProviderSource (SearXNG/Brave) — NO PROVIDERS CONFIGURED
       c. FixtureSource (texas_procurement.json) — RETURNS DATA
    2. Filter by state, city, expanded industry keywords
    3. Build ConnectorResult with confidence, metadata
    4. Return results marked data_source="fixture", bridge_mode=true
```

### 3.3 Company Validation (Final Stage)

```
validate_companies():
    For each company:
        1. _is_valid_name() — non-empty, ≥3 chars, contains alpha
        2. _is_live_website() — HTTP HEAD request, 2xx = live
        3. _is_blocked_domain() — wikipedia, .gov, .edu, linkedin, github, etc.
    Dead website → confidence -= 0.15, mark unverified
    Invalid name/blocked domain → REJECT entirely
```

---

## 4. Discovery Pipeline Components

### 4.1 Plugin System (`app/discovery/plugins/`)

- **BaseDiscoveryPlugin** — Abstract interface with `discover(industry, location, limit) → (SourceStatus, companies, metadata)`
- **PluginCapability** — Open enum: `COMPANY_DISCOVERY`, `LEADERSHIP_DISCOVERY`, `EMAIL_DISCOVERY`, `WEBSITE_DISCOVERY`, etc.
- **PluginManager** — Executes plugins, isolates failures, tracks per-plugin metrics & health
- **PluginRegistry** — Singleton registry for plugin registration
- **PluginSource** — Adapts plugin framework to `BaseSource` for SourceOrchestrator
- **attach_plugin_source()** — Optional wiring; logs decision; NOOP when no plugins registered

**Status:** Framework complete. **No concrete plugins implemented yet.** (Sprint 2.3 target: `CompanyDirectoryPlugin`)

### 4.2 Source System (`app/discovery/sources/`)

- **BaseSource** — Abstract interface matching plugin contract
- **SourceStatus** — `SUCCESS`, `EMPTY`, `UNAVAILABLE`, `ERROR`
- **SourceOrchestrator** — Registers sources by priority, executes sequentially, aggregates, deduplicates, determines `data_source` (`live`/`fixture`/`empty`)
- **Concrete Sources:**
  - `FixtureSource` — Emergency bridge (priority=999)
  - `SearchProviderSource` — Wraps SearchProviderManager (priority=50)
  - `PluginSource` — Wraps PluginManager (priority=40)
  - *Future:* `SOSBusinessSource`, `CMBLSource`, `CountyProcurementSource`

### 4.3 Search Providers (`app/search_providers/`)

- **BaseSearchProvider** — Abstract interface with `search(SearchQuery) → SearchResponse`
- **SearchProviderManager** — Executes providers in priority order, aggregates, deduplicates by URL, fallback on failure
- **Providers:**
  - `SearXNGProvider` — Self-hosted metasearch (priority=10, requires `SEARXNG_URL`)
  - `BraveSearchProvider` — REST API (priority=20, requires `BRAVE_SEARCH_API_KEY`)
- **ContractorClassifier** — AI/classifier to filter search results for contractor companies

**Status:** Providers implemented. **No providers configured** (env vars not set). Returns empty gracefully.

### 4.4 Connectors (`app/connectors/`)

- **BaseConnector** — Abstract interface: `search(industry, location, limit) → (ConnectorResult[], metadata)`, `health_check()`, `validate_result()`
- **ConnectorManager** — Loads enabled connectors from registry, sorts by priority, executes, dedupes, ranks
- **ConnectorRegistry** — Singleton auto-registration on import
- **IndustryExpansion** — Maps industry terms to expanded keyword sets
- **CompanyNormalizer** — Strips suffixes, normalizes URLs, standardizes locations

**Implemented Connectors:**
- `TexasProcurementConnector` (priority=10) — Fixture bridge + optional live search
- `AgcTexasConnector` (priority=20) — AGC Texas directory
- *Planned:* `CompanyDirectoryConnector` (priority=20) — YellowPages, BBB, chamber directories

### 4.5 Website Discovery (`app/discovery/website/`)

**New in Sprint 2.3A** — Direct website extraction framework:

- **Extraction Interfaces** (`extractors.py`) — 7 abstract extractors: `CompanyNameExtractor`, `PhoneExtractor`, `EmailExtractor`, `AddressExtractor`, `LeadershipExtractor`, `SocialExtractor`, `ServicesExtractor`
- **Evidence Model** (`evidence.py`) — `FieldEvidence` (per-field provenance: page_url, selector, method, confidence, snippet), `EvidenceSet` (accumulates observations per field)
- **Confidence Model** (`confidence.py`) — Validated `Confidence` value object (0.0–1.0, immutable, orderable)
- **URL Normalizer** (`url_normalizer.py`) — `normalize_url()` (fetchable canonical), `canonical_key()` (scheme-insensitive dedup key), `extract_host()`
- **URL Filter** (`url_filter.py`) — `DuplicateURLFilter` — first-seen-wins dedup with stats (accepted, duplicates, invalid)

### 4.6 Crawlers (`app/crawlers/`)

- **HTTPCrawler** — Main orchestrator: session management, rate limiting, retry, robots.txt, caching, HTML parsing
- **Components:**
  - `SessionManager` — aiohttp session pooling
  - `RateLimiter` — Token bucket per host + global
  - `RetryEngine` — Exponential backoff (2s, 4s, 8s max)
  - `RobotsManager` — robots.txt fetch/parse/cache (TTL 1hr)
  - `HTMLParser` — BeautifulSoup extraction: title, meta, emails, phones, social, text, links, h1, meta keywords
  - `ResponseCache` — TTL-based in-memory cache

**Known Bug:** `robots.py` `_fetch_and_parse()` uses `loop.run_until_complete()` inside async context — **crashes** (Sprint 2.3 T1 blocker)

### 4.7 AI Components (`app/ai/`)

- **AIGateway** → **AIManager** → **Providers** (OpenAI, Anthropic, Gemini, Local/Ollama)
- **AIScorer** — Lead scoring via AI
- **AISummarizer** — Text summarization
- **Prompts** — Cold email, company summary, lead score templates

**Isolation Rule:** AI is a leaf node; never calls back into engines/connectors.

---

## 5. Dependency Analysis

### 5.1 Core Dependencies

| Dependency | Purpose | Version |
|------------|---------|---------|
| FastAPI | API framework | Latest |
| SQLAlchemy 2.x | ORM | Latest |
| Pydantic v2 | Validation | Latest |
| aiohttp | Async HTTP | Latest |
| BeautifulSoup4 | HTML parsing | Latest |
| FAISS | Embeddings | Latest |
| Ruff + Black | Linting/formatting | Latest |

### 5.2 Internal Module Relationships

```
app/
├── api/v1/              ← depends on: engines, schemas, database
├── engines/
│   ├── discovery/       ← depends on: connectors, (ai for future extraction)
│   ├── source_intelligence/  ← depends on: (internal)
│   └── source_connectors/    ← depends on: crawlers, (deprecated, migrating to connectors/)
├── connectors/          ← depends on: crawlers, industry_expansion, company_normalizer
├── crawlers/            ← depends on: (stdlib only - aiohttp, bs4)
├── discovery/
│   ├── plugins/         ← depends on: sources.status, plugin_config, plugin_health, plugin_metrics
│   ├── sources/         ← depends on: search_providers, fixtures
│   └── website/         ← depends on: (stdlib, url_normalizer, confidence, evidence)
├── search_providers/    ← depends on: (aiohttp)
├── ai/                  ← depends on: (provider SDKs)
├── research/            ← depends on: crawlers, ai
├── services/            ← depends on: engines, repositories, ai
├── repositories/        ← depends on: database, models
├── models/              ← depends on: database.base
└── schemas/             ← depends on: (pydantic)
```

### 5.3 Business Logic Ownership

| Business Logic | Owner Module |
|----------------|--------------|
| Discovery pipeline orchestration | `CompanyDiscoveryEngine` |
| Multi-connector execution & ranking | `ConnectorManager` |
| Connector-specific data fetching | Individual `BaseConnector` impls |
| Source orchestration & fallback | `SourceOrchestrator` |
| Plugin execution & health | `PluginManager` |
| Web search & classification | `SearchProviderManager`, `ContractorClassifier` |
| HTTP crawling infrastructure | `HTTPCrawler` + components |
| Website field extraction | `FieldExtractor` implementations (pending) |
| Company validation (live HEAD) | `validate_companies()` |
| Deduplication (domain/name/fuzzy) | `clean_companies()`, `ConnectorManager._deduplicate()` |
| Relevance ranking | `ConnectorManager._rank_results()` |
| Industry keyword expansion | `expand_industry()` |
| AI scoring/summarization | `AIScorer`, `AISummarizer` |
| Data persistence | `Repository` classes |

### 5.4 Infrastructure Modules (No Business Logic)

- `app/crawlers/` — Pure infrastructure (HTTP, retry, rate limit, robots, cache, parse)
- `app/search_providers/` — Infrastructure adapters for search APIs
- `app/ai/providers/` — Infrastructure adapters for AI APIs
- `app/database/` — SQLAlchemy engine/session
- `app/core/config.py` — Pydantic settings
- `app/repositories/` — Data access patterns

---

## 6. Current Project Status

### 6.1 Completed Phases

| Phase | Status | Notes |
|-------|--------|-------|
| Sprint 1.1 — Foundation | ✅ Done | FastAPI, DB, models, core config, logging |
| Sprint 1.2 — Architecture Freeze | ✅ Done | Clean Architecture enforced, no circular imports |
| Sprint 2.1 — Company Discovery Engine | ✅ Done | Engine, ConnectorManager, Connector framework, Texas Procurement |
| Sprint 2.2 — Connector SDK | 🔄 In Progress | Plugin framework, SourceOrchestrator, Website Discovery interfaces **complete**; **no live plugins implemented** |

### 6.2 Production-Ready Modules

| Module | Status | Notes |
|--------|--------|-------|
| `app/core/` | ✅ Production | Config, constants, exceptions, logging |
| `app/database/` | ✅ Production | SQLAlchemy base, session, migrations |
| `app/models/` | ✅ Production | Company, Contact, Research, Task, Campaign |
| `app/schemas/` | ✅ Production | Pydantic request/response schemas |
| `app/repositories/` | ✅ Production | Data access layer |
| `app/services/` | ✅ Production | Business logic orchestration |
| `app/engines/discovery/company/` | ✅ Production | Engine, validator, cleaner, models |
| `app/connectors/` | ✅ Production | Framework, registry, manager, normalizer, industry expansion |
| `app/search_providers/` | ✅ Production | Base, manager, Brave, SearXNG, models, registry, classifier |
| `app/crawlers/` | ⚠️ Has Blocker | **robots.py async bug** prevents async usage |
| `app/ai/` | ✅ Production | Gateway, manager, providers, scorer, summarizer |
| `app/discovery/plugins/` | ✅ Production | Framework complete, no concrete plugins |
| `app/discovery/sources/` | ✅ Production | Base, orchestrator, fixture, search_provider, plugin sources |
| `app/discovery/website/` | ✅ Production | Interfaces, evidence, confidence, URL utils |

### 6.3 Placeholder / Incomplete Modules

| Module | Status | What's Missing |
|--------|--------|----------------|
| `TexasProcurementConnector._fetch_live()` | Placeholder | Returns `[]`; needs real live search integration |
| `AgcTexasConnector` | Placeholder | Not fully implemented |
| `CompanyDirectoryConnector` | Not created | Sprint 2.3 target — YellowPages, BBB, chambers |
| Concrete Discovery Plugins | Not created | Need: `CompanyDirectoryPlugin`, `SOSBusinessPlugin`, etc. |
| Website Field Extractors | Interfaces only | 7 abstract extractors need implementations |
| `app/integrations/` | Placeholders | Apollo, Firecrawl, Hunter, LinkedIn, Playwright — directories exist, no code |
| `app/engines/source_connectors/` | Deprecated | Being migrated to `app/connectors/` |

### 6.4 Still Requiring Implementation

1. **Fix `crawlers/robots.py` async bug** (P0 blocker for Sprint 2.3)
2. **Implement `CompanyDirectoryConnector`** with live directory crawling
3. **Implement 7 `FieldExtractor` classes** for website extraction
4. **Register search providers** (configure `SEARXNG_URL`, `BRAVE_SEARCH_API_KEY`)
5. **Implement concrete discovery plugins** for government registries, trade directories
6. **Wire live search into Texas Procurement** (attempt live → fallback to fixture)
7. **End-to-end integration tests** with live sources
8. **Email/Leadership discovery pipelines** (downstream of discovery)

---

## 7. Architecture Quality Review

### 7.1 Strong Design Decisions

| Decision | Why It's Strong |
|----------|-----------------|
| **Provider-agnostic discovery engine** | Engine never imports connector logic; new sources = zero engine changes |
| **SourceOrchestrator with status contract** | Deterministic fallback (`SUCCESS`/`EMPTY`/`UNAVAILABLE`/`ERROR`) without any source being required |
| **Plugin framework = SourceOrchestrator adapter** | Phase 2.1 plugins integrate via `PluginSource` without touching orchestrator |
| **Open enum pattern** (`PluginCapability`, `ExtractedField`) | Future plugins can declare unknown capabilities/fields without framework changes |
| **Evidence-per-field model** | `FieldEvidence` + `EvidenceSet` makes extraction auditable, not just plausible |
| **Confidence as value object** | Validates 0.0–1.0 at construction; rejects NaN/bool silently becoming 1.0 |
| **URL canonicalization split** | `normalize_url()` (fetchable) vs `canonical_key()` (dedup identity) — correct separation |
| **Connector priority-based execution** | High-priority sources run first; fixture always last (priority=999) |
| **Comprehensive logging standard** | Every discovery execution logs providers found, selected, query, URLs, companies crawled/accepted/rejected, validation, ranking, fallback reason (CLAUDE.md §6) |
| **Dependency inversion** | API → Services → Engines/Connectors → Crawlers; AI is leaf node |

### 7.2 Weak Areas

| Area | Issue |
|------|-------|
| **No live discovery working** | Core value proposition not delivered; fixtures are primary source |
| **Crawler robots.py bug** | Blocks all async crawler usage (FastAPI routes, integration tests) |
| **Search providers unconfigured** | `SEARXNG_URL`, `BRAVE_SEARCH_API_KEY` not set → `SearchProviderSource` returns empty |
| **No concrete plugins** | Plugin framework exists but zero implementations |
| **Website extractors = interfaces only** | 7 extractors declared abstract; no implementations |
| **Texas Procurement = fixture-only** | `_fetch_live()` returns `[]`; live search attempted but no providers configured |
| **Duplicate dedup logic** | `ConnectorManager._deduplicate()` AND `clean_companies()` AND `SourceOrchestrator._deduplicate()` — three implementations |
| **Location parsing duplicated** | `_parse_location()` in `connector_manager.py`, `texas_procurement.py`, AND `_http.py` |
| **No integration test infrastructure** | Tests mock HTTP; no real end-to-end smoke tests against live sources |

### 7.3 Technical Debt

| Debt | Location | Impact |
|------|----------|--------|
| **Triple deduplication** | 3 locations | Inconsistent results possible; maintenance burden |
| **Triple location parsing** | 3 locations | Inconsistent parsing; bug surface area |
| **Deprecated `source_connectors/`** | `app/engines/source_connectors/` | Confusion; migration incomplete |
| **`_http.py` unused?** | `app/discovery/sources/_http.py` | Dead code? |
| **`app/discovery/` legacy** | `result_cleaner.py`, `people_parser.py` | Should be migrated/removed |
| **`app/integrations/` empty** | 6 directories, no code | Misleading; suggests functionality that doesn't exist |
| **Fixture JSON corruption risk** | `app/fixtures/texas_procurement.json` | ADR-002 mentions JSON corruption in production fixtures |

### 7.4 Duplicate Logic

1. **Deduplication** — `ConnectorManager._deduplicate()`, `SourceOrchestrator._deduplicate()`, `clean_companies()`
2. **Location parsing** — `ConnectorManager._parse_location()`, `TexasProcurementConnector._parse_location()`, `_http.py`
3. **Industry keyword extraction** — `ConnectorManager._extract_industry_keywords()`, `expand_industry()`, `_matches_industry()`
4. **URL domain extraction** — `ConnectorManager._extract_domain()`, `clean_companies._extract_domain()`, `SourceOrchestrator._deduplicate()`

### 7.5 Future Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| **Live sources blocked** (BBB, YellowPages) | Medium | High | Polite rate limiting; fallback to fixtures; log block events |
| **HTML structure changes** | Low | Medium | Robust CSS selectors + regex fallbacks; monitor tests |
| **Legal/ToS concerns** | Low | High | Only public non-auth pages; respect robots.txt; moderate rate |
| **Provider API changes** | Low | Medium | Provider abstraction; swap providers without engine changes |
| **Async bug spread** | Low | High | Thorough test suite; CI linting for sync-in-async patterns |
| **Multi-worker cache/rate-limit** | Medium | Medium | Redis-backed cache/rate-limiter for production (future sprint) |

### 7.6 Potential Bottlenecks

| Bottleneck | Location | Expected Impact |
|------------|----------|-----------------|
| **Sequential connector execution** | `ConnectorManager.discover()` | Total latency = sum of connector latencies; 30s budget |
| **Synchronous validation HEAD checks** | `validate_companies()` uses `requests.head()` | Blocks event loop; should use async HTTP |
| **In-memory cache/rate-limiter** | `ResponseCache`, `RateLimiter` | Not shared across uvicorn workers; cache miss on each worker |
| **AI extraction on every page** | Future extractors | Cost/latency prohibitive; must be selective |

---

## 8. Suggested Improvements

### 8.1 Immediate (Pre-Sprint 2.3)

1. **Fix `crawlers/robots.py` async bug** — Replace `loop.run_until_complete()` with `await _check()` in async context
2. **Consolidate deduplication** — Single `Deduplicator` class used by all layers
3. **Consolidate location parsing** — Single `_parse_location()` utility
4. **Remove dead code** — `_http.py`, legacy `app/discovery/`, deprecated `source_connectors/`
5. **Configure search providers** — Set `SEARXNG_URL` and/or `BRAVE_SEARCH_API_KEY` for live search

### 8.2 Sprint 2.3 Scope (Per Draft)

1. **Implement `CompanyDirectoryConnector`** — YellowPages, BBB, chamber directories
2. **Wire live search into Texas Procurement** — Attempt live via `SearchProviderManager` before fixture fallback
3. **Implement 7 `FieldExtractor` classes** — For direct website extraction
4. **End-to-end integration tests** — Real crawl with rate limiting
5. **Update API response** — Include `data_source` in metadata

### 8.3 Post-Sprint 2.3 (Future Sprints)

| Improvement | Target Sprint | Rationale |
|-------------|---------------|-----------|
| Concrete discovery plugins (SOS, CMBL, county) | 2.4+ | Real government sources |
| Redis-backed cache/rate-limiter | 2.4 | Multi-worker support |
| Async validation HEAD checks | 2.4 | Non-blocking event loop |
| AI-powered entity extraction | 2.4 | Better extraction from noisy HTML |
| Email discovery pipeline | 3.0 | Downstream enrichment |
| Leadership/persona discovery | 3.0 | Decision-maker identification |
| Database persistence of discovered companies | 3.0 | Separate concern from discovery |
| Playwright for JS-heavy sites | 2.4 (if needed) | When directories require JS |

### 8.4 Architecture Simplifications

1. **Unify deduplication** → Single `Deduplicator` service injected where needed
2. **Unify location parsing** → Single utility in `connectors/` or `core/`
3. **Deprecate `app/discovery/` legacy** → Move `result_cleaner.py`, `people_parser.py` or remove
4. **Complete `source_connectors/` → `connectors/` migration** — Remove deprecated package
5. **Remove empty `app/integrations/` directories** — Or implement them

---

## 9. Questions Requiring Clarification

| # | Question | Context |
|---|----------|---------|
| 1 | **Should `validate_companies()` use async HTTP?** | Currently uses blocking `requests.head()`. In FastAPI async context, this blocks the event loop. Should migrate to `aiohttp` or run in thread pool. |
| 2 | **What is the status of `app/engines/source_connectors/`?** | Marked "DEPRECATED — migrate to connectors/" in architecture.md. Is migration complete? Can we delete it? |
| 3 | **Is `app/discovery/sources/_http.py` used?** | Appears unused. Safe to remove? |
| 4 | **What is the plan for `app/integrations/`?** | 6 empty directories. Are these for future sprints or should they be removed? |
| 5 | **Should `CompanyDirectoryConnector` use the Plugin framework or stay as a Connector?** | Sprint 2.3 draft says "New Component: CompanyDirectoryConnector" as a `BaseConnector`. But Plugin framework exists. Which path? |
| 6 | **Is the `robots.py` fix approved?** | Sprint 2.3 T1 is a P0 blocker. Can we proceed with the fix? |
| 7 | **What are the acceptance criteria for "live discovery working"?** | Sprint 2.3 draft says ≥3 companies from live sources. Is this the right threshold? |
| 8 | **Should we consolidate the 3 deduplication implementations now or in a separate refactoring sprint?** | High technical debt but working. Refactoring risks regressions. |
| 9 | **What is the testing strategy for live sources?** | Integration tests against live BBB/YellowPages will be flaky. How to handle in CI? |
| 10 | **Is the `SEARXNG_URL` / `BRAVE_SEARCH_API_KEY` configuration available?** | Need these for `SearchProviderSource` to work. Are they in `.env` or need to be provisioned? |

---

## 10. Conclusion

LeadHunterPro has a **strong, well-architected foundation** with clear separation of concerns, provider-agnostic discovery, and extensible plugin framework. The architecture correctly implements the principles in CLAUDE.md.

**However, the system does not yet perform real discovery.** The critical path to production is:

1. **Fix the crawler async bug** (P0)
2. **Implement `CompanyDirectoryConnector`** with live directory crawling
3. **Configure search providers** (SearXNG/Brave)
4. **Wire live search into the pipeline** with proper fixture fallback
5. **Validate end-to-end** with real queries returning live-sourced companies

Once Sprint 2.3 is complete, the "Execute" button will perform **real company discovery** from the live web, with fixtures serving only as an emergency bridge — exactly as CLAUDE.md §10 mandates.

---

**Report Status:** COMPLETE  
**Next Step:** Awaiting approval to begin Sprint 2.3 implementation (starting with robots.py fix)