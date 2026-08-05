# LeadHunter Pro — Discovery Architecture

**Version:** 1.0  
**Status:** FINAL BLUEPRINT  
**Effective Date:** 2026-08-03  
**Supersedes:** All prior discovery design decisions (Sprint 2.1, Sprint 2.1R, Sprint 2.2)

---

## 1. Executive Summary

LeadHunter Pro discovers construction companies from public sources, enriches them with AI insights, scores fit, and produces outreach-ready reports.

The **Discovery Architecture** defined in this document governs how companies are found, validated, deduplicated, ranked, and handed off to downstream pipelines (enrichment, scoring, email discovery). Everything below is architectural intent — not implementation code. Implementation follows in subsequent sprints.

### Core Design Principle

> **The Discovery Engine never knows where data comes from.**
> It issues queries; connectors respond. The engine is blind to the source.

---

## 2. Complete Data Flow

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                           USER REQUEST                                       │
│  GET /api/v1/discovery/companies                                           │
│    ?industry=Roofing                                                       │
│     &location=Dallas Texas                                                 │
│      &limit=100                                                            │
└─────────────────────────┬──────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        LAYER 1: DISCOVERY ENGINE                             │
│                                                                              │
│  CompanyDiscoveryEngine.discover(                                            │
│      industry="Roofing",                                                    │
│      location="Dallas Texas",                                               │
│      limit=100,                                                             │
│  )                                                                           │
│                                                                              │
│  Responsibilities:                                                           │
│  ─────────────────                                                           │
│  • Parse location string into (city, state)                                  │
│  • Expand industry keywords (roofing → roof, shingle, gutter...)             │
│  • Fan-out: dispatch query to all enabled connectors                         │
│  • Collect results from each connector                                       │
│  • Pass aggregated results to Normalizer                                     │
│  • NEVER access raw data sources directly                                    │
│  • NEVER import connector-specific logic                                     │
│                                                                              │
│  Output: list[RawCompany] → to Normalizer                                   │
└─────────────────────────┬──────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        LAYER 2: CONNECTOR MANAGER                            │
│                                                                              │
│  ConnectorManager.dispatch(                                                 │
│      industry_expanded={"roof", "shingle", "gutter", ...},                  │
│      city="Dallas",                                                         │
│      state="TX",                                                            │
│      limit=100,                                                             │
│  )                                                                           │
│                                                                              │
│  Responsibilities:                                                           │
│  ─────────────────                                                           │
│  • Maintain registry of all enabled connectors                               │
│  • Sort connectors by priority (lower number = higher priority)              │
│  • Execute each connector sequentially (or in parallel with semaphore)       │
│  • Collect (list[ConnectorResult], metadata) from each                       │
│  • Aggregate metadata: total_raw, errors, timing per connector               │
│  • Forward results to Deduplicator                                           │
│  • NEVER know connector internals                                            │
│                                                                              │
│  Output: list[ConnectorResult] + metadata → to Deduplicator                 │
└─────────────────────────┬──────────────────────────────────────────────────┘
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
┌───────────────┐ ┌───────────────┐ ┌───────────────┐
│  CONNECTOR    │ │  CONNECTOR    │ │  CONNECTOR    │
│  TX Procurement│ │  AGC Texas    │ │  FUTURE #3    │
│               │ │               │ │               │
│  Implements   │ │  Implements   │ │  Implements   │
│  BaseSource   │ │  BaseSource   │ │  BaseSource   │
│  Interface    │ │  Interface    │ │  Interface    │
└───────┬───────┘ └───────┬───────┘ └───────┬───────┘
        │                 │                 │
        │   Each connector │                 │
        │   chooses its    │                 │
        │   own data load  │                 │
        │   strategy       │                 │
        │                 │                 │
        ▼                 ▼                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        LAYER 3: CONNECTOR-INTERNAL SOURCES                   │
│                                                                              │
│  Within each connector, the data flow is:                                    │
│                                                                              │
│  [Source] → [Crawler] → [Parser] → [AI Extractor (optional)] →              │
│  [Normalizer] → [Validator] → ConnectorResult[]                              │
│                                                                              │
│  Connectors MAY use any combination of these internal layers.                │
│  Connectors MUST NOT call upstream layers (Engine, Manager, Validator).      │
│                                                                              │
└─────────────────────────┬──────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        LAYER 4: DEDUPLICATOR                                 │
│                                                                              │
│  DeduplicationPolicy.deduplicate(                                           │
│      results=list[ConnectorResult],                                         │
│  )                                                                           │
│                                                                              │
│  Deduplication keys (in order):                                              │
│  ────────────────────────────                                                │
│  1. Normalized domain (hostname only, lowercase, no www.)                    │
│  2. Normalized company name (suffix-stripped, lowercase)                     │
│  3. Fuzzy name match (Jaccard ≥ 0.7 threshold)                              │
│                                                                              │
│  On duplicate: keep the result with highest confidence score.                │
│  Log every removal with reason.                                              │
│                                                                              │
│  Output: list[ConnectorResult] (deduped)                                    │
└─────────────────────────┬──────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        LAYER 5: RANKER                                       │
│                                                                              │
│  RelevanceRanker.rank(                                                      │
│      results=list[ConnectorResult],                                         │
│      search_industry=expanded_keywords,                                     │
│      search_city="dallas",                                                  │
│      search_state="TX",                                                     │
│  )                                                                           │
│                                                                              │
│  Scoring criteria:                                                           │
│  ─────────────────                                                           │
│  • State match:        +40 points                                           │
│  • City match:         +30 points                                           │
│  • Industry keyword:   +20 points                                           │
│  • Trusted source:     +10 points (verified gov/association)               │
│  • Live URL:           +5 points                                            │
│                                                                              │
│  Output: list[ConnectorResult] (sorted descending by score)                 │
└─────────────────────────┬──────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        LAYER 6: VALIDATOR                                    │
│                                                                              │
│  CompanyValidator.validate(                                                 │
│      results=list[ConnectorResult],                                         │
│      live_check=True,                    ← configurable                     │
│      blocklist=PUBLISHED_BLOCKLIST,         ← known non-commercial domains  │
│  )                                                                           │
│                                                                              │
│  Checks performed:                                                           │
│  ─────────────────                                                           │
│  1. Website resolves to 2xx (HTTP HEAD, async, concurrent)                  │
│  2. Company name is non-empty, ≥3 chars, contains alpha                     │
│  3. Domain is NOT in blocked list (wikipedia.org, .gov, .edu, etc.)         │
│                                                                              │
│  Failed validation:                                                          │
│  • live_check=True  → mark as "unverified", lower confidence by 0.2         │
│  • name/domain fail → reject entirely                                      │
│                                                                              │
│  Validation cache: domain → {status, checked_at}                            │
│  Cache TTL: 24 hours (stale entries re-checked on next query)               │
│                                                                              │
│  Output: list[ConnectorResult] (validated or downgraded)                    │
└─────────────────────────┬──────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        LAYER 7: OUTPUT TRUNCATION & METADATA                 │
│                                                                              │
│  Final truncation to requested limit.                                        │
│  Response includes:                                                          │
│  ─────────────────                                                           │
│  • companies: list[CompanyDiscoveryResult] (API schema)                      │
│  • metrics: DiscoveryMetrics (total_found, total_deduped, total_validated,   │
│             total_ranked, errors_by_source, timing)                          │
│                                                                              │
└─────────────────────────┬──────────────────────────────────────────────────┘
                          │
                          ▼
              ┌─────────────────────┐
              │   API RESPONSE      │
              │   200 OK            │
              │   JSON body         │
              └─────────────────────┘
                          │
                          │  (for companies that pass validation)
                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                     RESEARCH PIPELINE (Downstream)                           │
│                                                                              │
│  Discovered companies that pass validation are optionally enqueued for:      │
│                                                                              │
│  1. Website Research (crawling + AI summarization)                          │
│  2. Email Discovery (finding contact emails on website)                     │
│  3. Leadership Discovery (identifying key personnel)                        │
│  4. AI Enrichment (scoring, summary generation)                             │
│  5. Report Generation (outreach-ready dossiers)                             │
│                                                                              │
│  This pipeline is OUT OF SCOPE for Discovery Architecture.                   │
│  See separate research_pipeline.md for those details.                        │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Layer Responsibilities

### 3.1 Discovery Engine

**File:** `app/engines/discovery/company/company_discovery_engine.py`

**Single responsibility:** Orchestrate the discovery pipeline from user input to ranked results.

**Does:**
- Parse and validate user input (industry, location, limit)
- Expand industry keywords using `IndustryExpansion`
- Delegate to `ConnectorManager`
- Convert `ConnectorResult` → `CompanyDiscoveryResult` for API response
- Aggregate `DiscoveryMetrics`

**Does NOT:**
- Know anything about specific data sources
- Import connector implementations directly
- Access the database
- Call AI models
- Make HTTP requests

**Interface contract:**
```python
class CompanyDiscoveryEngine:
    def discover(
        self,
        industry: str,
        location: str,
        *,
        limit: int = 100,
        strict_mode: bool = False,
    ) -> tuple[list[CompanyDiscoveryResult], DiscoveryMetrics]:
        ...
```

---

### 3.2 Connector Manager

**File:** `app/connectors/connector_manager.py`

**Single responsibility:** Execute discovery across multiple registered connectors and aggregate results.

**Does:**
- Load enabled connectors from `ConnectorRegistry`
- Sort by priority
- Execute each connector's `search()` method
- Collect results and metadata
- Track errors per connector
- Apply global limit

**Does NOT:**
- Know how connectors fetch data
- Modify connector results (only delegates)
- Access the database

**Interface contract:**
```python
class ConnectorManager:
    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[ConnectorResult], dict[str, Any]]:
        ...
```

---

### 3.3 Connector (Base Interface)

**File:** `app/connectors/base_connector.py`

**Single responsibility:** Define the contract every data source must implement.

**Does:**
- Declare required interface (`search`, `health_check`, `validate_result`)
- Provide default `description`, `priority`, `enabled` attributes
- Supply a scoped logger via `ConnectorLogger`

**Does NOT:**
- Implement any data fetching
- Contain source-specific logic

**Interface contract:**
```python
class BaseConnector(ABC):
    connector_name: str          # e.g. "texas_procurement"
    priority: int = 100          # lower = higher priority
    enabled: bool = True         # toggle for enabling/disabling

    @abstractmethod
    def search(
        self,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[ConnectorResult], dict[str, Any]]:
        """Execute discovery. May use fixtures, API, scraping, or any method."""

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if this source is reachable/available."""

    @abstractmethod
    def validate_result(self, result: ConnectorResult) -> bool:
        """Validate a single result before it leaves this connector."""
```

**Connector freedom:** Each connector decides its own data-loading strategy:
- Fixture files (JSON)
- Public API calls
- HTML scraping via the Universal Crawler
- Database lookups
- Any combination

The Connector Manager and Discovery Engine are agnostic.

---

### 3.4 Universal Crawler

**File:** `app/crawlers/http_crawler.py`

**Single responsibility:** Fetch HTTP resources reliably and compliantly.

**Does:**
- Manage HTTP sessions with connection pooling
- Respect `robots.txt` before each request
- Apply rate limiting (per-host and global)
- Retry failed requests with exponential backoff
- Handle redirects transparently
- Enforce timeouts
- Rotate User-Agent strings

**Does NOT:**
- Parse HTML
- Extract data
- Know about connectors
- Access the database

**Interface contract:**
```python
@dataclass(frozen=True)
class CrawlRequest:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    timeout: int = 30
    follow_redirects: bool = True
    respect_robots: bool = True

@dataclass(frozen=True)
class CrawlResponse:
    status_code: int
    content: bytes
    url: str                    # final URL after all redirects
    headers: dict[str, str]
    successful: bool
    robots_compliant: bool      # False if robots.txt blocked this path

class BaseCrawler(ABC):
    async def crawl(self, request: CrawlRequest) -> CrawlResponse: ...
    async def close(self) -> None: ...
```

**Configuration:**
```python
CRAWLER_CONFIG = {
    "default_timeout": 30,
    "max_retries": 3,
    "retry_backoff_base": 2.0,       # seconds
    "retry_backoff_max": 30.0,       # cap
    "rate_limit_per_host_seconds": 0.5,
    "rate_limit_global_seconds": 0.1,
    "user_agent_pool": [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ...",
    ],
    "respect_robots_txt": True,
}
```

---

### 3.5 HTTP Client

**File:** `app/crawlers/http_client.py`

**Single responsibility:** Low-level HTTP session management for the crawler.

**Does:**
- Create and manage `aiohttp.ClientSession` instances
- Handle connection pooling
- Manage TLS verification
- Log request/response metadata (never bodies)

**Does NOT:**
- Implement business logic
- Parse responses
- Know about crawls or connectors

---

### 3.6 Robots Manager

**File:** `app/crawlers/robots.py`

**Single responsibility:** Determine whether a given URL is allowed to be crawled.

**Does:**
- Fetch and parse `robots.txt` for a host (cache result per host)
- Evaluate `Disallow` rules against requested paths
- Return `True` if allowed, `False` if blocked
- Handle malformed or unavailable `robots.txt` gracefully (allow by default)

**Cache strategy:** In-memory, keyed by host, TTL 1 hour. Stale entries trigger re-fetch.

**Does NOT:**
- Fetch URLs
- Parse HTML
- Make HTTP requests to target pages

---

### 3.7 Rate Limiter

**File:** `app/crawlers/rate_limiter.py`

**Single responsibility:** Enforce request frequency limits to avoid overloading targets.

**Algorithm:** Token bucket per host, with a global rate cap.

**Does:**
- Track requests per host within sliding time windows
- Block/sleep when rate limit is exceeded
- Log rate-limit events

**Does NOT:**
- Communicate with external services
- Persist state between process restarts (in-memory only)

---

### 3.8 Cache

**File:** `app/crawlers/cache.py`

**Single responsibility:** Store transient crawling and validation results to avoid redundant work.

**Cache layers:**

| Layer | Key | TTL | Purpose |
|---|---|---|---|
| robots.txt | host | 1 hour | Avoid re-parsing robots.txt |
| HTTP response | url | 10 minutes | Avoid re-fetching same page |
| Domain health | domain | 24 hours | Avoid repeated HEAD checks |
| Connector fixture | source_name | process lifetime | In-memory loaded fixtures |

**Implementation:** In-memory `dict` with TTL expiry. No persistence between process restarts (acceptable for a stateless FastAPI service; long-term persistence is a future sprint concern).

**Does NOT:**
- Persist to disk or database
- Share state across processes (not designed for multi-worker)

---

### 3.9 HTML Parser

**File:** `app/crawlers/html_parser.py`

**Single responsibility:** Extract structured data from raw HTML.

**Provides:**
- Title extraction
- Meta description extraction
- Email extraction (regex + mailto: links)
- Phone extraction (regex)
- Social media link collection
- Basic text content normalization
- Link extraction with canonical base URL resolution

**Does NOT:**
- Know about companies
- Make decisions about data relevance
- Call AI models

**Output contract:**
```python
@dataclass
class ParsedPage:
    url: str
    title: str
    description: str
    emails: list[str]
    phones: list[str]
    social_links: dict[str, str]    # {platform: url}
    text_content: str               # normalized, stripped
    links: list[str]                # absolute URLs found on page
```

---

### 3.10 AI Extraction Engine

**File:** `app/ai/extraction.py` (new)

**Single responsibility:** Use AI to extract structured company data from unstructured text or HTML when regex/heuristics are insufficient.

**When to use:**
- Extract company names from noisy HTML where regex fails
- Classify company industry from webpage text
- Extract address/location from freeform text
- Summarize company services from about pages

**When NOT to use:**
- Simple field extraction (use regex/parser instead — faster, cheaper, deterministic)
- Every page on every company (cost and latency would be prohibitive)

**Interface contract:**
```python
class AIExtractionEngine:
    def extract_company_profile(
        self,
        html: str,
        base_url: str,
        context: dict[str, str],   # {industry_hint, location_hint}
    ) -> dict[str, Any]:
        """Extract structured company data from HTML using AI."""

    def classify_industry(
        self,
        text: str,
    ) -> str:
        """Classify company industry from webpage text."""
```

**Design rule:** The AI extraction engine is called **by connectors**, never by the Discovery Engine. The engine remains AI-free.

---

### 3.11 Company Normalizer

**File:** `app/connectors/company_normalizer.py`

**Single responsibility:** Ensure consistent formatting across all connectors' output.

**Normalizes:**
- Company name: strip suffixes (Inc., LLC, Ltd.), collapse whitespace, title case
- Website: ensure https:// prefix, remove trailing slash, normalize www.
- Location: standardize city name casing, state code format (2-letter uppercase)
- Industry focus: lowercase, comma-separated keywords

**Does NOT:**
- Validate data quality
- Remove duplicates
- Make decisions about data correctness

---

### 3.12 Validator

**File:** `app/engines/discovery/company/company_validator.py`

**Single responsibility:** Verify that discovered companies represent real, reachable businesses.

**Checks (applied in order):**
1. Company name is non-empty and contains at least one alphabetic character
2. Website URL is syntactically valid
3. Website responds with 2xx (HTTP HEAD, concurrent, cached)
4. Domain is not in the blocklist

**Blocklist (maintained centrally):**
```python
BLOCKED_DOMAIN_PATTERNS = re.compile(
    r"(wikipedia\.org|\.gov$|\.edu$|linkedin\.com/in/|"
    r"github\.com/|facebook\.com/|twitter\.com/|instagram\.com/|"
    r"indeed\.com|monster\.com|angellist\.com)",
    re.IGNORECASE,
)
```

**Behavior on failure:**
- Name invalid → reject (hard fail)
- Website dead → downgrade confidence by 0.2, mark `verified=False`
- Blocked domain → reject (hard fail)

**Concurrency:** All HEAD checks run concurrently via `ThreadPoolExecutor` (max 20 concurrent).

---

### 3.13 Deduplicator

**File:** `app/engines/discovery/company/company_cleaner.py`

**Single responsibility:** Remove duplicate companies from the aggregated result set.

**Deduplication keys (all lowercased, suffix-stripped):**
1. **Domain** — extracted from website hostname
2. **Company name** — core name without corporate suffixes
3. **Fuzzy name match** — Jaccard similarity ≥ 0.7 threshold

**Conflict resolution:** When two entries are duplicates, keep the one with the highest `confidence` score. If equal, keep the one from the higher-priority source.

---

### 3.14 Ranker

**File:** `app/connectors/connector_manager.py` (method `_rank_results`)

**Single responsibility:** Score and sort discovered companies by relevance to the search query.

**Scoring rubric:**
| Criterion | Points |
|---|---|
| State matches search state | +40 |
| City matches search city | +30 |
| Industry keyword appears in company fields | +20 |
| Source is trusted (gov/association) | +10 |
| Website was verified live | +5 |

**Tie-breaking:** Higher confidence first, then earlier discovery timestamp.

---

### 3.15 Research Pipeline

**File:** `app/research/website/crawler.py`, `parser.py`, `analyzer.py` (existing + enhanced)

**Single responsibility:** After a company is discovered and validated, research its website for enrichment data.

**Pipeline stages:**
1. **Crawl** — Visit key pages (home, about, contact, services)
2. **Parse** — Extract emails, phones, social links, page content
3. **AI Analyze** — Generate company summary, identify leadership, assess fit
4. **Score** — Calculate composite lead score
5. **Store** — Write to database (companies, contacts, research tables)

**Design boundary:** The research pipeline is called **by services**, never by the discovery engine. The discovery engine returns raw company lists; services decide what to enrich.

---

## 4. Dependency Rules

### 4.1 Allowed Dependencies (Top-Down)

```
api/v1/              →  engines/  →  connectors/  →  crawlers/
                       services/       (internal)     (internal)
                       repositories/
                       models/
                       ai/
```

**Rule:** A layer may only depend on layers below it. Nothing flows upward.

### 4.2 Strictly Forbidden Dependencies

| From → To | Reason |
|---|---|
| `connectors/` → `engines/discovery/` | Circular dependency |
| `connectors/` → `api/` | Infrastructure leak |
| `crawlers/` → `engines/` | Circular dependency |
| `crawlers/` → `api/` | Infrastructure leak |
| `engines/` → `models/` directly | Must go through repositories |
| Any layer → `database/` except repositories | Separation of concerns |
| `ai/` → `engines/discovery/` | AI must never control discovery flow |
| `ai/` → `connectors/` | AI must never control data sourcing |

### 4.3 AI Isolation Rule

```
engines/discovery/     calls  →  ai/           ✓ ALLOWED
services/              calls  →  ai/           ✓ ALLOWED
connectors/            calls  →  ai/           ✓ ALLOWED (extraction only)
ai/                    calls  →  ANY engine    ✗ FORBIDDEN
ai/                    calls  →  api/          ✗ FORBIDDEN
```

The AI module is a leaf node. It receives prompts and returns text. It never triggers business logic.

---

## 5. The Company Object

Every layer in the discovery pipeline works with company data. Two representations exist:

### 5.1 ConnectorResult (Internal — Between Connectors and Manager)

```python
@dataclass(frozen=True)
class ConnectorResult:
    company_name: str           # Legal name as discovered
    website: str                # Canonical URL
    city: str                   # City from source
    state: str                  # State code (2-letter)
    country: str = "USA"
    source: str                 # Connector name (e.g. "texas_procurement")
    source_url: str = ""        # Original URL where found
    confidence: float = 1.0     # 0.0–1.0, source's self-assessed certainty
    metadata: dict[str, Any] = field(default_factory=dict)
        # Source-specific extra fields:
        #   - industry_focus: str
        #   - revenue_tier: str
        #   - employee_count: int (optional)
        #   - trade_category: str (e.g. "roofing", "plumbing")
```

### 5.2 CompanyDiscoveryResult (External — API Response)

```python
@dataclass(frozen=True)
class CompanyDiscoveryResult:
    company_name: str
    website: str
    city: str = ""
    state: str = ""
    country: str = "USA"
    source: str                 # e.g. "texas_procurement"
    confidence: float = 0.5
    source_url: str = ""
    discovery_reason: str = ""  # Human-readable explanation, e.g.
                                # "Matched 'roof' in industry_focus"
```

### 5.3 Conversion

The `CompanyDiscoveryEngine` converts `ConnectorResult` → `CompanyDiscoveryResult`:
- Maps `source` field
- Generates `discovery_reason` from matched keywords
- Preserves `confidence`
- Strips `metadata` (not exposed in API)

### 5.4 Database Company Model (Separate Entity)

```python
class Company(Base):
    id: int
    company_name: str
    website: str          # UNIQUE
    industry: str | None
    headquarters: str | None
    employee_count: int | None
    ai_score: int = 0
    research_completed: bool = False
    created_at: datetime
```

This is the persistence model, used by the Research Pipeline and Services. It is separate from the in-flight discovery models.

---

## 6. Connector Lifecycle

### 6.1 Registration

```python
# At module import time (automatic)
ConnectorRegistry.register(TexasProcurementConnector())
ConnectorRegistry.register(AgcTexasConnector())

# Or programmatically
from app.connectors.connector_registry import ConnectorRegistry
ConnectorRegistry.register(MyNewConnector())
```

Registry stores: `{connector_name: connector_instance}`

### 6.2 Execution Flow

```
ConnectorManager.discover()
    │
    ├─► ConnectorRegistry.get_enabled()     # Filter enabled=True
    ├─► sorted(by priority ascending)       # Highest priority first
    │
    └─► For each connector:
            ├─► connector.is_available()    # Quick health check
            │       └─► skip if False
            ├─► connector.search(...)       # Execute discovery
            │       ├─► May use fixtures
            │       ├─► May use crawler
            │       ├─► May call AI extractor
            │       └─► Returns (results, metadata)
            └─► Collect results
```

### 6.3 Error Handling

- Individual connector failures are **caught and logged**, never raised
- Failed connectors contribute zero results but report error in metadata
- `ConnectorManager` continues with remaining connectors
- Final metadata includes per-connector error reporting

### 6.4 Health Check

Each connector implements `health_check()` which returns `True`/`False`. The Connector Manager skips connectors returning `False`. Health checks are lightweight (no full discovery run).

---

## 7. Crawler Lifecycle

### 7.1 Initialization

```python
crawler = HTTPCrawler(config=CRAWLER_CONFIG)
# Creates aiohttp session, initializes rate limiter, loads robots cache
```

### 7.2 Request Flow

```
connector.search()
    │
    ├─► robots.check(url)           # Is this URL allowed?
    │       └─► If blocked: skip, log warning
    │
    ├─► rate_limiter.acquire(host)  # Wait if limit exceeded
    │
    ├─► crawler.crawl(request)      # Fetch page
    │       ├─► Retry up to 3x with backoff
    │       ├─► Rotate User-Agent
    │       └─► Return CrawlResponse
    │
    ├─► html_parser.parse(response) # Extract structured data
    │
    └─► normalize + validate locally
```

### 7.3 Shutdown

```python
await crawler.close()
# Closes aiohttp session, releases connections
```

---

## 8. Future Scalability Strategy

### 8.1 Adding a New Source (Zero Engine Changes)

To add a new data source, a developer creates **one file**:

```python
# app/connectors/my_new_source.py
from app.connectors.base_connector import BaseConnector
from app.connectors.connector_result import ConnectorResult
from app.crawlers.http_crawler import HTTPCrawler

class MyNewSourceConnector(BaseConnector):
    connector_name = "my_new_source"
    priority = 50
    enabled = True

    def search(self, industry, location, limit):
        # Your data-fetching logic here
        # Can use self._crawler, self._parser, etc.
        ...
        return results, metadata

    def health_check(self) -> bool:
        ...

    def validate_result(self, result: ConnectorResult) -> bool:
        ...
```

Then register it:
```python
# In app/connectors/__init__.py
from app.connectors.my_new_source import MyNewSourceConnector
ConnectorRegistry.register(MyNewSourceConnector())
```

**That's it.** The Discovery Engine, Connector Manager, API, and tests require zero changes.

### 8.2 Scaling to Unlimited Sources

The architecture supports unlimited connectors because:
1. Registry is open — any module can register
2. Manager iterates dynamically — no hardcoded list
3. Results aggregate automatically — no schema changes needed
4. Each connector is independent — one failing doesn't break others

### 8.3 Horizontal Scaling (Multi-Worker)

For high-volume production:
- `ConnectorRegistry` can be replaced with a distributed registry (Redis-backed)
- `Crawler` sessions can be worker-local (each uvicorn worker gets its own)
- `Cache` can be replaced with Redis for shared state across workers
- `RateLimiter` can be Redis-backed for cross-worker coordination

These are optimizations for future sprints, not Sprint 2.2 concerns.

---

## 9. Performance Strategy

### 9.1 Concurrency Model

| Stage | Concurrency | Method |
|---|---|---|
| Connector execution | Sequential (with timeout) | Prevents resource exhaustion |
| Website HEAD checks | Concurrent (max 20) | `ThreadPoolExecutor` |
| Crawler requests | Per-host serialized, cross-host concurrent | Rate limiter enforces |
| HTML parsing | Sequential per page | CPU-bound, small payloads |

### 9.2 Timeout Budget

Total discovery request budget: **30 seconds**

Allocation:
- Connector execution: 15s total (shared across all connectors)
- Validation: 10s (concurrent HEAD checks)
- Overhead (parsing, ranking): 5s

If the budget is exceeded, partial results are returned with a `timeout_warning` in metadata.

### 9.3 Caching Strategy

| What | How Long | Why |
|---|---|---|
| robots.txt per host | 1 hour | Rarely changes |
| HTTP response | 10 minutes | Same page, same day |
| Domain health (HEAD) | 24 hours | Websites don't change hourly |
| Connector fixtures | Process lifetime | Static data, loaded once |

### 9.4 Connector Timeout

Each connector has a hard timeout (configurable, default 10s). If a connector exceeds its timeout, it is cancelled and logged. Remaining connectors continue. This prevents one slow source from blocking the entire pipeline.

---

## 10. Retry Strategy

### 10.1 Crawler-Level Retries

```
Attempt 1: Initial request
Attempt 2: Backoff 2s
Attempt 3: Backoff 4s
Attempt 4: Backoff 8s (max 3 retries)
```

Retried on: `ConnectionError`, `Timeout`, `5xx` status codes.

Not retried on: `4xx` status codes (client error), `429` (rate limited — wait longer).

### 10.2 Connector-Level Retries

Connectors do not auto-retry their own data sources. If a connector fails, the Connector Manager logs the error and moves to the next connector. Retry logic belongs to the connector's internal implementation (e.g., the crawler handles its own retries).

### 10.3 Validation Retries

Dead website checks are NOT retried. A failed HEAD is cached as "unverified" for 24 hours. Re-checking immediately would be wasteful.

---

## 11. Error Handling Strategy

### 11.1 Error Classification

| Level | Example | Action |
|---|---|---|
| **Fatal** | Invalid API config, missing DB | Service fails to start |
| **Hard** | Connector throws unexpected exception | Log error, skip connector, continue |
| **Warning** | 404 on website, empty result set | Log warning, include in metadata |
| **Info** | Rate limit hit, robots blocked | Log info, continue |

### 11.2 Error Propagation

Errors never propagate out of connectors to the engine. Every connector wraps its `search()` in `try/except` and returns `([], {"error": str(e)})` on failure.

The Connector Manager aggregates errors into metadata:
```python
{
    "errors": {
        "my_connector": "HTTP 520 from upstream",
        "another_connector": "Connection refused",
    },
    "warnings": [
        "Connector 'legacy_source' skipped: health_check failed",
    ],
}
```

### 11.3 API Error Responses

| Scenario | HTTP Status | Body |
|---|---|---|
| Missing/invalid query params | 422 | Pydantic validation error |
| All connectors failed | 200 | `{"companies": [], "metrics": {"errors": [...]}}` |
| Partial results | 200 | `{"companies": [...], "metrics": {"partial": true, ...}}` |
| Server crash (unexpected) | 500 | Standard FastAPI error |

**Key rule:** The API always returns 200 for discovery queries. Failure modes are expressed in the response body, not HTTP status codes. This allows clients to handle partial results gracefully.

---

## 12. Testing Strategy

### 12.1 Test Categories

| Category | Scope | Tools |
|---|---|---|
| Unit tests | Individual functions/classes in isolation | pytest + unittest.mock |
| Contract tests | Connectors implement BaseConnector correctly | pytest (interface assertions) |
| Integration tests | Connector → Manager → Engine pipeline | pytest (mocked HTTP) |
| End-to-end tests | Full request cycle through API | pytest + httpx AsyncClient |
| Fixture tests | Curated dataset integrity | pytest (data validation) |

### 12.2 Mocking Strategy

All external dependencies are mocked:
- HTTP requests → `unittest.mock.patch` on `aiohttp.ClientSession`
- Database → SQLAlchemy in-memory SQLite (where needed)
- AI providers → stub returning fixed text
- Filesystem → `tmp_path` fixture for temp fixtures

### 12.3 Test Coverage Requirements

| Module | Minimum Coverage | Required Tests |
|---|---|---|
| Discovery Engine | 90%+ | Happy path, empty results, timeout, multi-connector |
| Connector Manager | 90%+ | Fan-out, dedup, ranking, error aggregation |
| Each Connector | 85%+ | search(), health_check(), validate_result(), edge cases |
| Crawler | 90%+ | Success, retry, rate limit, robots block, timeout |
| Validator | 95%+ | Live/dead sites, blocked domains, name validation |
| Deduplicator | 95%+ | Domain dup, name dup, fuzzy dup, no dup, mixed |
| Ranker | 90%+ | State match, city match, industry match, ties |
| Industry Expansion | 95%+ | Known expansion, unknown term, empty input |

---

## 13. Folder Structure (Production-Grade)

```
backend/app/
│
├── api/v1/                           # HTTP routes (thin layer)
│   ├── __init__.py
│   ├── router.py                     # Aggregates all sub-routers
│   ├── discovery.py                  # GET /discovery/companies
│   ├── connectors.py                 # GET /connectors/, /connectors/{name}
│   ├── company.py                    # CRUD for stored companies
│   ├── research.py                   # Research pipeline endpoints
│   ├── email.py                      # Email discovery endpoints
│   ├── leadership.py                 # Leadership discovery endpoints
│   ├── health.py                     # /health
│   └── database.py                   # /database/status
│
├── crawlers/                         # SHARED infrastructure (new)
│   ├── __init__.py
│   ├── base.py                       # BaseCrawler ABC, CrawlRequest, CrawlResponse
│   ├── http_crawler.py               # aiohttp-based crawler
│   ├── html_parser.py                # BeautifulSoup extraction helpers
│   ├── robots.py                     # robots.txt compliance
│   ├── rate_limiter.py               # Token-bucket rate limiter
│   ├── cache.py                      # TTL-based in-memory cache
│   └── http_client.py                # Low-level session manager
│
├── connectors/                       # Connector framework + implementations
│   ├── __init__.py
│   ├── base_connector.py             # BaseConnector ABC
│   ├── connector_registry.py         # Singleton registry
│   ├── connector_result.py           # ConnectorResult dataclass
│   ├── connector_manager.py          # Orchestrates multi-connector discovery
│   ├── company_normalizer.py         # Name/URL/location normalization
│   ├── industry_expansion.py         # Trade keyword expansion mapping
│   ├── texas_procurement.py          # Texas procurement connector
│   ├── agc_texas.py                  # AGC Texas connector
│   └── example.py                    # Template for new connectors
│
├── engines/
│   ├── __init__.py
│   ├── discovery/
│   │   ├── __init__.py
│   │   └── company/
│   │       ├── __init__.py
│   │       ├── company_discovery_engine.py   # Main orchestrator
│   │       ├── company_models.py             # Data classes
│   │       ├── company_cleaner.py            # Deduplication
│   │       └── company_validator.py          # Website validation
│   ├── source_intelligence/              # (unchanged)
│   └── source_connectors/                # DEPRECATED — migrate to connectors/
│
├── ai/                                 # AI module (leaf node, never called back)
│   ├── __init__.py
│   ├── gateway.py                      # AIGateway
│   ├── manager.py                      # AIManager
│   ├── scorer.py                       # Lead scoring
│   ├── summarizer.py                   # Text summarization
│   ├── extraction.py                   # AI-powered data extraction
│   ├── providers/                      # Provider implementations
│   └── prompts/                        # Prompt templates
│
├── research/
│   ├── __init__.py
│   └── website/
│       ├── __init__.py
│       ├── crawler.py                  # Existing — website research crawler
│       ├── parser.py                   # Existing — HTML parsing for research
│       └── analyzer.py                 # Existing — AI analysis
│
├── email/                              # Email discovery (unchanged)
│   ├── __init__.py
│   ├── email_discovery.py
│   └── email_validator.py
│
├── discovery/                          # Legacy discovery utilities (migrate out)
│   ├── __init__.py
│   ├── result_cleaner.py
│   └── people_parser.py
│
├── scoring/                            # Company scoring (unchanged)
│   ├── __init__.py
│   └── company_score.py
│
├── services/                           # Business logic orchestration
│   ├── __init__.py
│   ├── company_service.py
│   ├── research_service.py
│   ├── email_service.py
│   ├── leadership_service.py
│   └── crawler_service.py
│
├── repositories/                       # Data access (unchanged)
│   ├── __init__.py
│   ├── company_repository.py
│   ├── contact_repository.py
│   └── research_repository.py
│
├── models/                             # ORM models (unchanged)
│   ├── __init__.py
│   ├── company.py
│   ├── contact.py
│   ├── research.py
│   └── campaign.py
│
├── schemas/                            # Pydantic schemas (unchanged)
│   ├── __init__.py
│   ├── company.py
│   ├── contact.py
│   └── research.py
│
├── core/                               # Configuration (unchanged)
│   ├── __init__.py
│   ├── config.py
│   ├── constants.py
│   ├── logging.py
│   └── exceptions.py
│
├── database/                           # Database setup (unchanged)
│   ├── __init__.py
│   ├── base.py
│   ├── session.py
│   └── database.py
│
├── reports/                            # Report generation (unchanged)
│   ├── __init__.py
│   └── builder.py
│
└── main.py                             # FastAPI application
```

---

## 14. Layer-by-Layer Dependency Map

```
                    ┌─────────────────────────────────────┐
                    │           api/v1/                   │
                    │   (depends on: engines, schemas)    │
                    └──────────────┬──────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────┐
                    │         engines/                    │
                    │   (depends on: connectors, crawlers │
                    │             ai, repositories)        │
                    └──────┬──────────────┬───────────────┘
                           │              │
          ┌────────────────┼──────────────┼────────────────┐
          │                │              │                │
          ▼                ▼              ▼                ▼
  ┌───────────────┐ ┌─────────────┐ ┌──────────┐ ┌─────────────┐
  │ discovery/    │ │ source_     │ │ ai/      │ │ repositories│
  │ company/      │ │ intelligence│ │          │ │             │
  └───────┬───────┘ └──────┬──────┘ └────┬─────┘ └──────┬──────┘
          │                │              │              │
          └────────────────┼──────────────┼──────────────┘
                           │              │
              ┌────────────▼──────┐ ┌─────▼──────┐
              │  connectors/      │ │ crawlers/  │
              │  (independent)    │ │ (indep.)   │
              └───────────────────┘ └────────────┘
                           │
              ┌────────────▼────────────┐
              │    infrastructure       │
              │  (base, result, cache,  │
              │   rate_limiter, robots) │
              └─────────────────────────┘
```

**No upward arrows permitted.** If you find one, it is an architecture violation.

---

## 15. Connector vs. Legacy Code Migration Map

### 15.1 What Moves

| Current Location | Destination | Status |
|---|---|---|
| `engines/source_connectors/texas_procurement.py` | `connectors/texas_procurement.py` | Migrate (new implementation) |
| `engines/source_connectors/agc_texas/` | `connectors/agc_texas.py` | Migrate (refactor) |
| `engines/source_connectors/mock_connector.py` | `connectors/example.py` | Rename + update |
| `engines/source_connectors/deduplicator.py` | `engines/discovery/company/company_cleaner.py` | Consolidate |
| `engines/source_connectors/error_handler.py` | `crawlers/` (utility) | Move |
| `engines/source_connectors/connector_logger.py` | `connectors/` (utility) | Keep where it is |
| `engines/source_connectors/http_client.py` | `crawlers/http_client.py` | Replace with new |
| `engines/source_connectors/rate_limiter.py` | `crawlers/rate_limiter.py` | Move |
| `engines/source_connectors/normalizer.py` | `connectors/company_normalizer.py` | Rename + expand |
| `engines/source_connectors/retry_manager.py` | `crawlers/http_crawler.py` (internal) | Inline |
| `engines/source_connectors/sdk.py` | `connectors/base_connector.py` + `connector_result.py` | Merge + deprecate |

### 15.2 What Stays

| Current Location | Reason |
|---|---|
| `engines/discovery/company/company_discovery_engine.py` | Core orchestrator, correct architecture |
| `engines/discovery/company/company_cleaner.py` | Deduplication logic, correct |
| `engines/discovery/company/company_validator.py` | Validation logic, needs async enhancement only |
| `engines/discovery/company/company_models.py` | Data classes, correct |
| `research/website/*` | Separate pipeline, not part of discovery |
| `email/*` | Separate pipeline |
| `ai/*` | Leaf node, already isolated correctly |
| `services/*` | Orchestration layer, correct |
| `repositories/*` | Data access, correct |
| `models/*` | ORM, correct |
| `schemas/*` | API contracts, correct |
| `scoring/*` | Post-discovery, correct |
| `reports/*` | Post-discovery, correct |

### 15.3 What Gets Deleted

| File | Reason |
|---|---|
| `connectors/adapters.py` | Adapter indirection eliminated — connectors implement BaseConnector directly |
| `engines/source_connectors/sdk.py` | Superseded by `connectors/base_connector.py` |
| `engines/source_connectors/texas_procurement.py` | Replaced by new connector |
| `engines/source_connectors/agc_texas/` (entire dir) | Replaced by new connector |
| `discovery/result_cleaner.py` | Superseded by `company_cleaner.py` |
| `discovery/people_parser.py` | Superseded by AI extraction engine + parser |

---

## 16. Summary of Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Single SDK vs. dual SDK | **Single** | Two registries cause confusion; one `BaseConnector` eliminates adapter layer |
| Sync vs. async crawler | **Async** | aiohttp enables concurrent requests; better throughput |
| Fixture-first vs. scrape-first | **Fixture-first, scrape-ready** | No freely-scrapable sources exist; fixtures provide immediate value; crawler layer ready for when APIs open |
| Connection-per-connector vs. shared | **Shared crawler instance** | Connection pooling across connectors reduces total outgoing connections |
| Hard fail vs. soft fail on connector error | **Soft fail** | One broken source shouldn't kill the entire discovery |
| HTTP status for partial results | **Always 200** | Clients should handle partial data gracefully; errors in body |
| AI in discovery vs. post-discovery | **Post-discovery only** | Discovery must be fast and deterministic; AI is for enrichment |
| Database writes during discovery | **None** | Discovery returns ephemeral results; writes happen in research pipeline |
| Cache persistence | **In-memory only** | Acceptable for stateless API; complexity of persistent cache deferred |
| Industry matching | **Expanded keywords** | "Roofing" must match "roof repair" and "shingle contractor" |
