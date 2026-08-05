# Sprint 2.2 — Real Discovery Foundation

**Status:** READY  
**Priority:** CRITICAL  
**Owner:** Hanzlah  
**Reference:** [docs/sprints/source_validation_report.md](source_validation_report.md)  
**Preceded by:** [docs/architecture/review_discovery_redesign.md](../architecture/review_discovery_redesign.md)

---

## Objective

Transform the Company Discovery Engine from a **40-entry fixture filter** into a **real discovery foundation** with:
- Expanded curated datasets (500+ contractors across all trades and Texas cities)
- Reusable crawler infrastructure for future live-source integration
- Industry-expansion matching so trade queries (Roofing, Plumbing, Electrical) return results
- Unified single-SDK connector framework
- End-to-end tests proving the pipeline works

This sprint delivers the **foundation** — not live scraping (see Source Validation Report for why). Future sprints will add live data sources once API access or partnerships are secured.

---

## Background

### What Went Wrong

Sprint 2.1 built a Company Discovery Engine using Google/Bing/DuckDuckGo/Serper/SerpAPI. These providers are blocked or rate-limited, so the system pivoted to connectors. However, the connectors implemented discovery by filtering a static list of ~40 enterprise general contractors — not by discovering companies from any source.

Manual QA revealed:

| Query | Expected | Actual |
|---|---|---|
| Roofing, Texas | Companies doing roofing work | `[]` |
| Plumbing, Texas | Plumbing contractors | `[]` |
| Electrical, Texas | Electrical contractors | `[]` |
| limit=20 | Up to 20 companies | 3–5 |
| limit=100 | Up to 100 companies | 3–5 |
| Repeated searches | Varied results | Same 40 entries every time |

### Root Cause

The `_TAXAS_CONSTRUCTION_COMPANIES` constant contains exactly 40 large enterprise general contractors. None specialize in trades. The industry keyword filter requires exact substring match — "roofing" matches nothing. Two parallel SDK systems add architectural confusion.

### Source Validation Findings

A comprehensive evaluation of 13 candidate data sources was completed (see [Source Validation Report](source_validation_report.md)). Key findings:

| Source | Tier | Why |
|---|---|---|
| TxDOT Vendor Database | **Tier 3** | 403 on robots.txt, Cloudflare WAF, login required |
| AGC Texas Directory | **Tier 3 (live)** | 403 on robots.txt, WAF. Curated fixture data OK (Tier 1) |
| Harris/Dallas/Austin County Bids | **Tier 2** | Public record but BidBoard ToS restrictions; save for Sprint 2.3 |
| Texas Licensing Board | **Tier 2** | Excellent quality but technically complex (POST forms, no bulk API) |
| BuildZoom | **Tier 3** | Cloudflare Enterprise, paid API only |
| ENR | **Tier 3** | Paywalled media platform |
| ThomasNet | **Tier 3** | Paid API, heavy JS rendering |
| SageBill | **Tier 3** | Subscription-required |
| ABC Texas | **Tier 3** | Domain DNS failure |
| Yellow Pages / Superpages | **Tier 3** | Poor data quality |
| LinkedIn | **Tier 3** | Severe legal risk, account bans |

**Conclusion:** No freely scrapable, legally clean, high-volume source exists for Texas construction companies at this time. Every viable path forward requires either paid API access, formal partnership, or browser automation complexity beyond Sprint 2.2 scope.

### Revised Strategy: Hybrid Approach

1. **Curated fixture datasets** — Expand from ~40 to 500+ entries covering all trades (roofing, plumbing, electrical, HVAC, concrete, flooring, painting, etc.) across all major TX cities. Sourced from public records (county bid awards, license databases).
2. **Crawler infrastructure** — Build reusable `app/crawlers/` so future live sources plug in cleanly when available.
3. **Industry expansion** — Map trade keywords so "Roofing" finds "roof repair," "shingle," "gutter" etc.
4. **Connector unification** — Single SDK, single registry, no adapter indirection.

The fixtures are the **foundational dataset**, not a compromise. Every serious B2B lead platform starts this way and adds live sourcing as partnerships mature.

---

## Architecture

### Target Pipeline

```
User Search (industry + location + limit)
    │
    ▼
┌─────────────────────────────────────┐
│  CompanyDiscoveryEngine.discover()  │
│  - Parse location → (city, state)   │
│  - Fan-out to enabled connectors    │
└──────────────┬──────────────────────┘
               │
       ┌───────┼───────┐
       ▼       ▼       ▼
┌──────────┐ ┌──────────┐ ┌──────────────┐
│ TX Proc. │ │ AGC TX   │ │ (Future)     │
│ (500+    │ │ (300+    │ │ Trade Dir.   │
│  fixtures)│ │  fixtures)│ │ (T2 source)  │
└────┬─────┘ └────┬─────┘ └──────┬───────┘
     │            │               │
     └────────────┼───────────────┘
                  ▼
      ┌─────────────────────────┐
      │  Normalizer             │
      │  (unify field names)    │
      └───────────┬─────────────┘
                  ▼
      ┌─────────────────────────┐
      │  Deduplicator           │
      │  (by domain + name)     │
      └───────────┬─────────────┘
                  ▼
      ┌─────────────────────────┐
      │  Ranker                 │
      │  (state/city/industry   │
      │   match scoring)        │
      └───────────┬─────────────┘
                  ▼
      ┌─────────────────────────┐
      │  Validator              │
      │  (live website check)   │
      │  (async, cached)        │
      └───────────┬─────────────┘
                  ▼
         Return results
```

### Component Responsibilities

| Component | Location | Responsibility |
|---|---|---|
| HTTP Crawler | `app/crawlers/http_crawler.py` | Session-managed fetching with retries, rate limiting — for future live sources |
| HTML Parser | `app/crawlers/html_parser.py` | BeautifulSoup extraction helpers — for future live sources |
| Robots Checker | `app/crawlers/robots.py` | robots.txt compliance — for future live sources |
| Base Crawler | `app/crawlers/base.py` | ABC defining crawler interface — for future live sources |
| Industry Expansion | `app/connectors/industry_expansion.py` | Map trade terms to keyword sets |
| Texas Procurement Connector | `app/connectors/texas_procurement.py` | Load 500+ curated fixtures, apply filters + expansion |
| AGC Texas Connector | `app/connectors/agc_texas.py` | Load 300+ curated fixtures, apply filters |
| ConnectorManager | `app/connectors/connector_manager.py` | Orchestrate fan-out, dedup, rank |
| CompanyDiscoveryEngine | `app/engines/discovery/company/company_discovery_engine.py` | Pipeline orchestrator |
| Validation | `app/engines/discovery/company/company_validator.py` | Website health check (async) |
| Cleaner | `app/engines/discovery/company/company_cleaner.py` | Deduplication |

### Unified Connector Contract

All connectors implement a single `BaseConnector` from `app/connectors/base_connector.py`:

```python
class BaseConnector(ABC):
    connector_name: str
    priority: int          # lower = higher priority
    enabled: bool

    @abstractmethod
    def search(self, industry: str, location: str, limit: int) -> tuple[list[ConnectorResult], dict[str, Any]]: ...

    @abstractmethod
    def health_check(self) -> bool: ...

    @abstractmethod
    def validate_result(self, result: ConnectorResult) -> bool: ...
```

Legacy types (`ConstructionSourceConnector`, `CompanyResult`) in `app/engines/source_connectors/sdk.py` are deprecated with warnings. New connectors target `BaseConnector` / `ConnectorResult`.

---

## Modules

### Module 1: Crawler Layer (`app/crawlers/`)

Reusable infrastructure for future live-source connectors. Not tied to any specific source in Sprint 2.2 — built for readiness.

```
app/crawlers/
├── __init__.py
├── base.py              # BaseCrawler ABC, CrawlRequest, CrawlResponse
├── http_crawler.py      # aiohttp session, retry, rate limit, timeout
├── html_parser.py       # BeautifulSoup helpers for company record extraction
└── robots.py            # robots.txt compliance checker
```

**Key contracts:**

```python
@dataclass(frozen=True)
class CrawlRequest:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    timeout: int = 30
    follow_redirects: bool = True

@dataclass(frozen=True)
class CrawlResponse:
    status_code: int
    content: bytes
    url: str                    # final URL after redirects
    headers: dict[str, str]
    successful: bool

class BaseCrawler(ABC):
    async def crawl(self, request: CrawlRequest) -> CrawlResponse: ...
    async def close(self) -> None: ...
```

**Features:**
- Session with connection pooling
- Automatic retry with exponential backoff (max 3 attempts)
- Configurable rate limiting (minimum 200ms between requests)
- robots.txt compliance check
- Timeout enforcement
- User-Agent rotation pool
- Proper error handling (log, don't crash)

---

### Module 2: Industry Expansion (`app/connectors/industry_expansion.py`)

Maps trade categories to searchable keyword sets, enabling queries like "Roofing Texas" to match companies listed under "Roof Repair" or "Gutter Services."

```python
INDUSTRY_EXPANSION: dict[str, list[str]] = {
    "roofing": ["roof", "roofing", "shingle", "gutter", "roof repair", "tile roof", "roofing contractor"],
    "plumbing": ["plumbing", "plumber", "pipe", "hvac", "heating", "cooling", "water heater", "plumbing contractor"],
    "electrical": ["electrical", "electrician", "wiring", "electrical contractor", "panel", "electrical services"],
    "hvac": ["hvac", "heating", "cooling", "air conditioning", "furnace", "ventilation", "hvac contractor"],
    "general_contractor": ["general contractor", "gc", "contracting", "construction", "builder", "general builders"],
    "commercial_construction": ["commercial construction", "commercial builder", "estimating", "cost consulting"],
    "residential": ["residential", "home builder", "remodeling", "renovation", "residential builder"],
    "concrete": ["concrete", "paving", "driveway", "foundation", "concrete contractor"],
    "flooring": ["flooring", "carpet", "tile floor", "hardwood", "floor installation"],
    "painting": ["painting", "paint", "staining", "paint contractor"],
    "landscaping": ["landscaping", "lawn", "irrigation", "hardscape", "landscape"],
}

def expand_industry(industry: str) -> set[str]:
    """Expand an industry term into a set of searchable keywords."""

def matches_industry(company_text: str, industry: str) -> bool:
    """Check if company description/name matches an industry query (expanded)."""
```

---

### Module 3: Texas Procurement Connector (`app/connectors/texas_procurement.py`)

Replaces the fixture-based legacy connector. Loads expanded curated dataset from `fixtures/texas_procurement.json`.

**Dataset requirements:**
- **500+ companies** minimum
- **Coverage:** All trades (roofing, plumbing, electrical, HVAC, concrete, flooring, painting, landscaping, general contracting, commercial construction, residential)
- **Geography:** All major TX cities (Dallas, Houston, Austin, San Antonio, Fort Worth, El Paso, Arlington, Plano, Corpus Christi, Laredo)
- **Data fields per entry:** company_name, website, city, state, country, source_url, industry_focus, revenue_tier, trade_category
- **Source:** Compiled from public county bid award records, published contractor lists, and trade association directories

**Behavior:**
- On each `search()` call, load fixtures, apply expanded industry matching, filter by location
- Return `list[ConnectorResult]` with source metadata indicating "fixture" source
- Log filter statistics (total in dataset, matched, returned)
- Never fabricate data — only return verified companies

---

### Module 4: AGC Texas Connector (`app/connectors/agc_texas.py`)

Refactored from `app/engines/source_connectors/agc_texas/`. Loads curated member fixture data.

- Uses new crawler layer for any future live fetch (currently fixture-only)
- No silent fixture fallback — if fixtures are missing, fail loudly with clear error
- Returns `list[ConnectorResult]` with source metadata

---

### Module 5: Connector Consolidation

- Remove `app/connectors/adapters.py` — connectors implement `BaseConnector` directly
- Deprecate `app/engines/source_connectors/sdk.py` with warnings
- Single `ConnectorRegistry` in `app/connectors/connector_registry.py`
- Single `BaseConnector` in `app/connectors/base_connector.py`
- Keep SDK utilities (`HTTPClient`, `RateLimiter`, `Deduplicator`, `ErrorHandler`, `Normalizer`) as internal helpers within connectors

---

## Folder Structure

### After Implementation

```
backend/
├── app/
│   ├── crawlers/                           # NEW
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── http_crawler.py
│   │   ├── html_parser.py
│   │   └── robots.py
│   ├── connectors/
│   │   ├── __init__.py                     # MODIFIED — new imports
│   │   ├── base_connector.py               # UNCHANGED
│   │   ├── connector_registry.py           # UNCHANGED
│   │   ├── connector_result.py             # UNCHANGED
│   │   ├── connector_manager.py            # MODIFIED — use industry expansion
│   │   ├── company_normalizer.py           # UNCHANGED
│   │   ├── industry_expansion.py           # NEW
│   │   ├── texas_procurement.py            # NEW — 500+ fixture-based connector
│   │   ├── agc_texas.py                    # NEW — refactored AGC connector
│   │   └── adapters.py                     # DELETED
│   ├── engines/
│   │   ├── discovery/
│   │   │   └── company/
│   │   │       ├── company_discovery_engine.py   # UNCHANGED
│   │   │       ├── company_cleaner.py            # UNCHANGED
│   │   │       ├── company_validator.py          # MODIFIED — async validation
│   │   │       └── company_models.py             # MODIFIED — clean SUPPORTED_SOURCES
│   │   └── source_connectors/            # DEPRECATED — kept for backward compat
│   │       ├── sdk.py                    # MODIFIED — deprecation warnings
│   │       ├── texas_procurement.py      # DEPRECATED
│   │       ├── agc_texas/                # DEPRECATED
│   │       └── ...utilities...           # KEPT
│   ├── api/v1/
│   │   ├── discovery.py                  # MODIFIED — update docstring
│   │   └── connectors.py                 # MODIFIED — route through ConnectorManager
│   └── fixtures/                         # NEW
│       ├── texas_procurement.json        # 500+ curated companies
│       └── agc_texas_members.json        # 300+ curated members
└── tests/
    ├── crawlers/                         # NEW
    │   ├── __init__.py
    │   ├── test_http_crawler.py
    │   ├── test_html_parser.py
    │   └── test_robots.py
    ├── connectors/
    │   ├── test_base_connector.py        # UNCHANGED
    │   ├── test_connector_registry.py    # UNCHANGED
    │   ├── test_connector_result.py      # UNCHANGED
    │   ├── test_connector_manager.py     # MODIFIED
    │   ├── test_texas_procurement.py     # NEW
    │   ├── test_agc_texas.py             # NEW
    │   └── test_industry_expansion.py    # NEW
    ├── discovery/
    │   ├── test_company_discovery.py     # RESTORED
    │   ├── test_deduplicator.py          # UNCHANGED
    │   ├── test_error_handler.py         # UNCHANGED
    │   ├── test_http_client.py           # UNCHANGED
    │   ├── test_mock_connector.py        # MODIFIED
    │   └── ...                           # OTHERS UNCHANGED
    └── fixtures/                         # NEW
        ├── txdot_vendor_sample.json      # Sample for mocked tests
        └── agc_texas_sample.html         # Sample markup for parser tests
```

---

## Deliverables

### Code

1. **Crawler layer** — `app/crawlers/` (5 files) with full async support, retry, rate limiting
2. **Industry expansion module** — `app/connectors/industry_expansion.py`
3. **Texas Procurement connector** — `app/connectors/texas_procurement.py` (500+ curated fixtures)
4. **AGC Texas connector** — `app/connectors/agc_texas.py` (refactored)
5. **Consolidated connector framework** — single `BaseConnector`, single registry
6. **Async validator** — `company_validator.py` with threaded validation and domain cache
7. **Deprecated legacy SDK** — `app/engines/source_connectors/sdk.py` with deprecation warnings

### Data

8. **Expanded Texas Procurement fixture** — 500+ companies covering all trades and major TX cities
9. **Expanded AGC Texas fixture** — 300+ members
10. **Sample mock data** — For integration tests (TxDOT-style response, AGC HTML snippet)

### Tests

11. Crawler unit tests (mocked HTTP)
12. HTML parser unit tests (sample markup)
13. Industry expansion unit tests
14. Texas Procurement integration tests (fixture loading, filtering, expansion)
15. AGC Texas integration tests
16. Restored `test_company_discovery.py` — engine-level integration tests
17. End-to-end test: "Roofing Texas" returns valid results
18. End-to-end test: "Plumbing Houston" returns valid results
19. End-to-end test: "Electrical Austin" returns valid results

### Documentation

20. Updated [connector_sdk.md](../architecture/connector_sdk.md)
21. Crawler layer docstring documentation
22. Connector authoring guide (how to add a new source)
23. Updated [CLAUDE.md](../../CLAUDE.md) — sprint status, removed dead search provider references

---

## Acceptance Criteria

- [ ] `GET /api/v1/discovery/companies?industry=Roofing&location=Dallas Texas&limit=20` returns ≥5 valid companies
- [ ] `GET /api/v1/discovery/companies?industry=Plumbing&location=Houston Texas&limit=20` returns ≥5 valid companies
- [ ] `GET /api/v1/discovery/companies?industry=Electrical&location=Austin Texas&limit=20` returns ≥5 valid companies
- [ ] `GET /api/v1/discovery/companies?industry=General Contractor&location=TX&limit=50` returns ≥15 valid companies
- [ ] `GET /api/v1/discovery/companies?industry=Roofing&location=TX&limit=100` returns ≤100 companies, no duplicate domains
- [ ] `GET /api/v1/connectors/` lists active connectors with descriptions
- [ ] No `print()` statements in any production or test code
- [ ] `pytest` passes with zero failures across all test directories
- [ ] `ruff check .` passes with zero violations
- [ ] `black .` formatting applied
- [ ] No circular imports (`python -c "from app.main import app"` succeeds)
- [ ] Swagger at `/docs` loads without errors
- [ ] Every registered connector implements `BaseConnector` (no legacy `ConstructionSourceConnector`)
- [ ] Fixture datasets cover all major trades and major TX cities
- [ ] Industry expansion correctly maps trade queries to matching companies

---

## Definition of Done

Claude must:

1. Implement all deliverables above.
2. Run `ruff check .` — fix all violations.
3. Run `black .` — apply formatting.
4. Run `pytest` — fix all failures.
5. Repeat steps 2–4 until clean.
6. Start FastAPI server (`python -m uvicorn app.main:app --reload`) — verify startup and routing.
7. Run the manual QA plan below.
8. Update all documentation.
9. Generate sprint report.

**Only then** mark this sprint as complete. Never leave TODO comments or known bugs.

---

## Manual QA Plan

Run these queries against the running server and verify results:

| # | Query | Expected |
|---|---|---|
| 1 | `GET /api/v1/discovery/companies?industry=Roofing&location=Dallas Texas&limit=20` | ≥5 companies, each with valid website field |
| 2 | `GET /api/v1/discovery/companies?industry=Plumbing&location=Houston Texas&limit=20` | ≥5 companies |
| 3 | `GET /api/v1/discovery/companies?industry=Electrical&location=Austin Texas&limit=20` | ≥5 companies |
| 4 | `GET /api/v1/discovery/companies?industry=General Contractor&location=TX&limit=50` | ≥15 companies |
| 5 | `GET /api/v1/discovery/companies?industry=Roofing&location=TX&limit=100` | ≤100 companies, no duplicate domains |
| 6 | `GET /api/v1/connectors/` | Lists Texas Procurement and AGC Texas with descriptions |
| 7 | `GET /api/v1/connectors/texas-procurement` | Returns connector metadata including dataset size |
| 8 | `GET /api/v1/discovery/companies?industry=Concrete&location=San Antonio Texas&limit=10` | ≥3 companies (tests cross-city trade matching) |
| 9 | Check logs | No `print()` output; structured logging at INFO level; shows filter stats |
| 10 | Check Swagger | `/docs` loads; all endpoints documented with correct schemas |
| 11 | Run same query twice | Identical results (deterministic fixture data), metadata shows source |
| 12 | Search for nonexistent trade + nonexistent city | Returns `[]` with clear metadata (not an error) |

---

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Fixture dataset curation is time-consuming (500+ entries) | Medium | Use programmatic generation from public data patterns; validate sample by hand |
| Fixture data becomes stale over time | Medium | Add `last_updated` timestamp to metadata; document refresh cadence for future sprints |
| Industry expansion creates false positives | Low | Conservative keyword lists; log matches; allow tightening via query parameter |
| Dual SDK migration breaks existing tests | Medium | Keep adapters as thin shim during transition; run full suite after each change |
| Async validator introduces concurrency bugs | Medium | Thread-safe domain cache; write focused unit tests |
| No live sources means results are bounded by dataset size | High (inherent) | Document clearly; plan live-source integration for Sprint 2.3 with secured APIs |

---

## Migration Plan

### Phase 1: Foundation (Days 1–2)

1. Create `app/crawlers/` package with `BaseCrawler`, `HTTPCrawler`, parsers
2. Write unit tests for crawler (mocked aiohttp)
3. Create `app/connectors/industry_expansion.py`
4. Write unit tests for industry expansion
5. Verify `ruff`, `black`, `pytest` pass with no regressions

### Phase 2: Fixture Curation (Days 2–3)

1. Generate 500+ Texas Procurement fixture entries covering all trades and cities
2. Generate 300+ AGC Texas fixture entries
3. Validate fixtures: each has valid website field, realistic company name, correct state
4. Add sample mock data for integration tests
5. Verify fixture loading works correctly

### Phase 3: Connector Rewrite (Days 4–5)

1. Implement `app/connectors/texas_procurement.py` (loads fixtures, applies expansion + filters)
2. Refactor `app/connectors/agc_texas.py` (uses new structure)
3. Remove `app/connectors/adapters.py`
4. Deprecate `app/engines/source_connectors/sdk.py` types
5. Update `ConnectorManager` to use industry expansion in ranking
6. Run full test suite

### Phase 4: Pipeline Hardening (Day 6)

1. Make `company_validator.py` async with domain cache
2. Update `company_models.py` — remove dead provider names from `SUPPORTED_SOURCES`
3. Restore `test_company_discovery.py` with engine integration tests
4. End-to-end QA against all acceptance criteria
5. Update documentation

---

## File Change Summary

### Files Created (15)

| File | Purpose |
|---|---|
| `app/crawlers/__init__.py` | Package init |
| `app/crawlers/base.py` | BaseCrawler ABC, request/response models |
| `app/crawlers/http_crawler.py` | Async HTTP crawler with retry, rate limit |
| `app/crawlers/html_parser.py` | BeautifulSoup extraction helpers |
| `app/crawlers/robots.py` | robots.txt compliance |
| `app/connectors/industry_expansion.py` | Industry keyword expansion mapping |
| `app/connectors/texas_procurement.py` | Curated-fixture Texas connector (500+ entries) |
| `app/connectors/agc_texas.py` | Refactored AGC Texas connector |
| `app/fixtures/texas_procurement.json` | 500+ curated Texas construction companies |
| `app/fixtures/agc_texas_members.json` | 300+ AGC Texas members |
| `tests/crawlers/__init__.py` | Test package init |
| `tests/crawlers/test_http_crawler.py` | Crawler unit tests |
| `tests/crawlers/test_html_parser.py` | Parser unit tests |
| `tests/crawlers/test_robots.py` | Robots compliance tests |
| `tests/connectors/test_texas_procurement.py` | Texas connector integration tests |
| `tests/connectors/test_agc_texas.py` | AGC connector integration tests |
| `tests/connectors/test_industry_expansion.py` | Industry expansion unit tests |
| `tests/fixtures/txdot_vendor_sample.json` | Sample TxDOT response for mocked tests |
| `tests/fixtures/agc_texas_sample.html` | Sample AGC HTML for parser tests |

### Files Modified (8)

| File | Change |
|---|---|
| `app/connectors/base_connector.py` | Add `discover()` alias for backward compat |
| `app/connectors/connector_manager.py` | Use industry expansion in ranking logic |
| `app/connectors/__init__.py` | Import new connectors, remove adapter references |
| `app/engines/discovery/company/company_validator.py` | Async concurrent validation with domain cache |
| `app/engines/discovery/company/company_models.py` | Clean `SUPPORTED_SOURCES` literal; remove dead provider names |
| `app/engines/source_connectors/sdk.py` | Add deprecation warnings |
| `app/api/v1/discovery.py` | Update docstring (remove dead-provider/CAPTCHA references) |
| `app/api/v1/connectors.py` | Route through ConnectorManager |
| `CLAUDE.md` | Update sprint status, remove dead search provider mentions |

### Files Deleted (7)

| File | Reason |
|---|---|
| `app/connectors/adapters.py` | Eliminated — connectors implement BaseConnector directly |
| `app/engines/source_connectors/texas_procurement.py` | Superseded by new curated-fixture connector |
| `app/engines/source_connectors/agc_texas/__init__.py` | Superseded |
| `app/engines/source_connectors/agc_texas/config.py` | Superseded |
| `app/engines/source_connectors/agc_texas/connector.py` | Superseded |
| `app/engines/source_connectors/agc_texas/parser.py` | Superseded |
| `app/engines/source_connectors/agc_texas/normalizer.py` | Superseded |

---

## Why This Sprint Is Required

### 1. The Original Sprint 2.2 Was Obsolete

The original sprint scoped SDK polishing while the discovery engine returned `[]` for trade-specific queries. Finishing that sprint would deliver a well-documented framework that still produces no results.

### 2. Source Validation Changed Everything

The Source Validation Report proves that no free, legal, scrapable source exists at scale for Texas construction companies. The sprint had to be redesigned around what is actually achievable: **curated fixtures as the foundation, crawler infrastructure for the future.**

### 3. Discovery Is the Core Value Proposition

LeadHunter Pro's vision is "discovers construction companies from public sources." If the system cannot find a roofing contractor in Texas, it has failed its primary function regardless of how clean the connector SDK is.

### 4. Technical Debt Accumulation

The dual-SDK architecture (two registries, two base classes, adapter glue) is a maintenance trap. Consolidating now prevents compounding debt while the foundation is being rebuilt anyway.

---

## Expected Benefits

| Before | After |
|---|---|
| Roofing Texas → `[]` | Roofing Texas → ≥5 real companies |
| Plumbing Texas → `[]` | Plumbing Texas → ≥5 real companies |
| Electrical Texas → `[]` | Electrical Texas → ≥5 real companies |
| limit=100 → 3–5 results | limit=100 → up to 100 companies |
| Same 40 companies every query | 500+ diverse companies from curated data |
| Generic discovery reasons | Specific trade-matched discovery reasons |
| Two SDKs to maintain | One canonical connector framework |
| No crawler infrastructure | Reusable `app/crawlers/` ready for live sources |
| No industry expansion | "Roofing" matches "roof repair", "shingle", etc. |

---

## Implementation Complexity

| Area | Complexity | Notes |
|---|---|---|
| Crawler layer | Medium | Standard patterns; well-understood |
| Industry expansion | Small | Table lookups + string matching |
| Fixture curation (500+ entries) | Large | Most time-intensive step; requires systematic data compilation |
| AGC Texas refactor | Medium | Structural change, straightforward |
| Connector consolidation | Medium | Careful migration; keep adapters as shim initially |
| Async validator | Medium | Threading + caching introduces complexity |
| Testing | Medium | Mocked HTTP required; fixture validation tests |

Overall: **Medium-High complexity**, primarily due to fixture curation volume and connector consolidation carefulness. The architectural direction is clear; the main effort is data collection.

---

**End of Sprint 2.2 — Real Discovery Foundation**
