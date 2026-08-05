# LeadHunter Pro — Engineering Principles

**Document Type:** Permanent Engineering Constitution  
**Effective Date:** 2026-08-03  
**Version:** 1.0  
**Authority:** Founder & AI Engineering Team  
**Review Cycle:** Annual, or upon architectural inflection point

---

## 1. Purpose

These principles define the engineering standards that govern every line of code, every architectural decision, and every sprint deliverable in LeadHunter Pro. They exist because a platform that discovers construction companies from public sources demands discipline in how that discovery is built -- not just what it discovers.

These are **permanent rules**, not preferences. They apply to every sprint, every contributor, and every file in the `backend/` directory. When a decision conflicts with a principle, the principle wins.

They were forged through observed failures: search providers that broke under production load, fixture-based "discovery" that returned empty results for trade-specific queries, dual SDKs that confused every new developer, and silent data degradation that went unnoticed until manual QA exposed it. Each principle below addresses a specific failure encountered during development.

Read them before every sprint. Follow them always.

---

## 2. Engineering Principles

### 2.1 Single Responsibility Principle

Every module has exactly one responsibility. A file that mixes concerns is a bug waiting to surface.

| Module | Responsibility |
|---|---|
| `api/v1/` | HTTP routing, input validation, response formatting |
| `services/` | Business logic orchestration |
| `engines/discovery/` | Orchestrating the discovery pipeline |
| `engines/source_intelligence/` | Intelligence planning from discovered sources |
| `connectors/` | Individual data source implementations |
| `crawlers/` | HTTP fetching, parsing, compliance |
| `ai/` | AI model invocation and prompt management |
| `scoring/` | Lead score calculation |
| `repositories/` | Database access |
| `models/` | ORM class definitions |
| `schemas/` | Pydantic request/response contracts |

**Rule:** If a module does two things, split it. There are no exceptions.

### 2.2 Separation of Concerns

The system is layered. Each layer communicates only with the layer immediately below it.

```
api/v1/        -->  services/        -->  engines/         -->  connectors/
                                                                  (data sourcing)
                                                          -->  crawlers/          (fetching)
                                                          -->  utilities/         (shared helpers)
```

**What this means in practice:**

- API routes never contain business logic. They receive a request, validate it, call a service or engine, and return the response.
- Engines never access the database directly. They call repositories.
- Connectors never call the Discovery Engine. They return data; the engine consumes it.
- Crawlers never know about connectors. They fetch bytes; connectors parse them.
- Utility modules (rate limiter, deduplicator, normalizer) have no dependencies on business layers.

**Violation pattern to watch for:** An import that flows upward in the diagram above. If you find one, refactor it immediately.

### 2.3 Dependency Direction

Dependencies always flow downward, never upward.

```
Allowed:     api --> services --> engines --> connectors --> crawlers --> utilities
Forbidden:   crawlers --> engines,  engines --> api,  utilities --> services
```

**Enforcement mechanism:** The build fails if circular imports are detected. Every new import must be justified by asking: "Does this module sit above the imported module in the dependency hierarchy?"

**No exceptions.** A utility that depends on an engine is an engine that should be refactored into a utility.

### 2.4 Replaceable Components

Every connector must be removable without affecting any other part of the system. Every AI provider must be interchangeable.

This means:
- Connectors implement a stable interface (`BaseConnector`). The engine calls methods, not implementations.
- AI providers implement a stable interface. The engine calls `generate()`, not provider-specific methods.
- No connector hard-codes another connector's behavior.
- No engine hard-codes a specific data source URL or format.
- Removing a connector leaves the system operational with remaining connectors.

**Test this rule:** Can you delete `texas_procurement.py` and rerun all tests with zero failures? If not, something is too tightly coupled. Fix it before proceeding.

### 2.5 Production Data Policy

**Static fixtures are allowed ONLY for:**
- Unit tests (isolated, deterministic, fast)
- Integration tests (mocked external responses)
- CI/CD pipelines (reproducible build verification)

**Production discovery MUST NEVER depend on static fixtures.**

This principle was established after manual QA revealed that the system returned `[]` for trade-specific queries because a 40-entry hardcoded list contained no roofing, plumbing, or electrical contractors. The fix is not to add more fixtures -- it is to connect to real data sources.

**What counts as a production data source:**
- Public APIs (official, documented endpoints)
- Publicly accessible websites (robots.txt compliant, rate-limited)
- Government databases (license lookups, bid records)
- Trade association directories (with permission or public access)
- Aggregator platforms (with API licensing)

**What does NOT count:**
- Hardcoded lists in Python source files
- JSON files committed to the repository (except test fixtures under `tests/fixtures/`)
- Manually typed company records not sourced from public records

**Transition period:** During sprints where live sources are not yet available (per Source Validation Report), curated fixture datasets sourced from public records may be used as a bridge. These datasets must include `data_provenance` metadata documenting each entry's origin. A subsequent sprint must replace the bridge with a live source before the bridge sprint is marked complete.

### 2.6 Error Handling

Errors must never silently disappear.

**Rule 1: Log everything.** Every error path must emit a structured log at the appropriate level (`WARNING` for recoverable issues, `ERROR` for failures). Use `logging.getLogger(__name__)` -- never `print()`.

**Rule 2: Never swallow exceptions.** A bare `except:` clause is prohibited. Catch specific exceptions. Log the cause. Re-raise or return a structured error response.

**Rule 3: Retry external requests.** Every call to an external system (HTTP, database, AI provider) must support retries with exponential backoff. Maximum 3 retries. Backoff base: 2 seconds, max cap: 30 seconds.

**Rule 4: Fail open, not closed.** When a single connector fails, the system continues with remaining connectors. The API returns partial results with error metadata, not a 500 status code. The discovery pipeline degrades gracefully -- it never crashes because one source is unavailable.

**Rule 5: Distinguish transient from permanent failures.** Connection timeouts and 5xx responses are retried. 4xx client errors (404, 403, 422) are logged and skipped -- retrying them wastes resources.

### 2.7 Scalability

The architecture must scale without redesign. Specifically:

- **100 connectors:** The registry supports unlimited registrations. Adding a connector requires one file and one registry line.
- **1,000 connectors:** Priority-based execution and timeout isolation prevent one slow connector from blocking others.
- **Millions of companies:** Deduplication uses domain-level keys (not full-record comparison). Ranking uses O(n log n) sort. Validation uses concurrent HTTP HEAD with caching.

**Design choices that enable this:**
- Results are aggregated at the manager level, not per-connector
- The validator caches domain health checks (24-hour TTL) to avoid repeated HEAD requests
- The deduplicator uses hashed keys for O(1) lookups
- Connectors execute sequentially with individual timeouts -- no global lock

**Anti-pattern to avoid:** Loading all results into memory before any processing begins. For large-scale operations, implement streaming or pagination at the connector level.

### 2.8 Testability

Every module must be testable in isolation. Every public interface must have tests.

**Requirements:**
- All external dependencies (HTTP, database, AI providers) are mockable
- No global state leaks between tests (each test gets a fresh registry instance)
- Tests run in under 30 seconds total (fast feedback loop)
- No test makes a real network request -- all HTTP is mocked

**Test coverage targets:**
- Discovery Engine: 90%+
- Connector Manager: 90%+
- Each Connector: 85%+
- Crawler: 90%+
- Validator: 95%+ (this is the quality gate)
- Deduplicator: 95%+
- Industry Expansion: 95%+

**Quality gates (must pass before any commit):**
```bash
ruff check .          # Zero violations
black .               # Formatting applied
pytest                # All tests green
python -c "from app.main import app"   # No import errors
```

### 2.9 Documentation First

No production feature is complete without corresponding documentation updates.

**The documentation cascade:**

| Change Type | Required Documentation Update |
|---|---|
| New module or major refactor | Architecture document section |
| Architectural decision | ADR in `docs/decisions/` |
| New connector | Connector authoring guide update |
| API change | API docstring + OpenAPI schema |
| Bug fix | QA note in relevant test file |
| New data source | Source Validation Report update |
| Sprint completion | Sprint report in `docs/sprints/` |

**The rule is simple:** If you changed the architecture, the architecture document must reflect it. If you made a decision between alternatives, an ADR must record why. If users interact with your change through the API, the API docs must describe it.

Documentation is not an afterthought. It is part of the definition of done.

### 2.10 No Rewrite Rule

When the architecture is found to be incorrect, the correct response is **not** to patch around it. The correct response is:

1. Document the misalignment in an engineering review
2. Write an ADR explaining the current state and desired state
3. Design the correction as a migration plan (phased, non-breaking)
4. Execute the migration sprint by sprint
5. Verify each phase before proceeding to the next

Patching bad architecture creates compound debt. Fixing it once, properly, saves ten times the effort over the lifetime of the project.

**Evidence that the architecture needs revision:**
- Two modules perform the same function with different interfaces (dual SDKs)
- Adding a new feature requires modifying three unrelated files
- Tests fail only when run together, not in isolation (coupling)
- The same bug appears in multiple places (shared implicit contract)
- Manual QA reveals fundamental mismatches between design and behavior

If any of these signs appear, stop implementing. Write the review. Fix the architecture. Then implement.

---

## 3. Code Quality Standards

### Language & Tooling

| Standard | Requirement | Enforcement |
|---|---|---|
| Python version | 3.12+ | `pyproject.toml` `requires-python` |
| Formatter | Black | Pre-commit hook |
| Linter | Ruff (zero tolerance) | Pre-commit hook + CI |
| Type hints | Mandatory on all functions | Ruff `ANN` rules |
| Docstrings | Google style, every public API | Ruff `D` rules |
| Logging | `logging.getLogger(__name__)` | Linter rule, no `print()` |
| Imports | Absolute, sorted by Ruff `I` | Ruff `I001` |

### Prohibited Patterns

| Pattern | Why | Alternative |
|---|---|---|
| `print()` in production | Pollutes stdout, bypasses logging | `logger.info/warning/error()` |
| Bare `except:` | Swallows all errors silently | Catch specific exceptions |
| Circular imports | Runtime failure, impossible to test | Restructure modules |
| Hard-coded credentials | Security risk | Environment variables via `Settings` |
| Raw SQL strings | Bypasses ORM safety | SQLAlchemy query builders |
| `TODO` comments | Technical debt that never gets paid | Resolve or create tracked issue |
| Placeholder implementations | False sense of progress | Remove or implement fully |
| Duplicate logic across connectors | Maintenance trap | Extract to shared utility |

---

## 4. Dependency Map

### Allowed Imports

```
api/v1/*          -->  services/*, schemas/*, engines/*
services/*        -->  engines/*, repositories/*, ai/*
engines/discovery/*  -->  connectors/*, crawlers/*, engines/discovery/company/*
engines/source_intelligence/*  -->  engines/discovery/*
connectors/*      -->  crawlers/*, core/*
crawlers/*        -->  core/*
ai/*              -->  core/*, models/*
repositories/*    -->  models/*, database/*
models/*          -->  database/base.py
schemas/*         -->  models/*
core/*            -->  (leaf -- no internal dependencies)
database/*        -->  (leaf -- no internal dependencies)
```

### Forbidden Imports

Any import that flows upward in the map above. Examples:
- `connectors/` importing from `engines/discovery/` -- circular
- `api/v1/` importing from `repositories/` -- skips layers
- `engines/` importing from `api/` -- infrastructure leak
- `crawlers/` importing from `connectors/` -- utility depending on consumer

---

## 5. Connector Contract

Every connector implements this interface. Nothing more, nothing less.

```python
class BaseConnector(ABC):
    connector_name: str          # Unique identifier, e.g. "texas_procurement"
    priority: int = 100          # Lower = higher priority in execution order
    enabled: bool = True         # Toggle to enable/disable without deleting

    @abstractmethod
    def search(self, industry: str, location: str, limit: int) -> tuple[list[ConnectorResult], dict[str, Any]]:
        """Execute discovery. May use fixtures, API, scraping, or any method."""

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if this source is reachable/available."""

    @abstractmethod
    def validate_result(self, result: ConnectorResult) -> bool:
        """Validate a single result before it leaves this connector."""
```

**Rules for connector implementation:**
1. `search()` must never raise an unhandled exception. Wrap all logic in try/except.
2. `health_check()` must be lightweight -- no full discovery run.
3. `validate_result()` is called by the Connector Manager before results are accepted.
4. Connectors must not import from `engines/discovery/` or `api/`.
5. Connectors may import from `crawlers/`, `core/`, and their own internal modules.

---

## 6. Testing Standards

### Test Organization

```
tests/
├── connectors/              # Connector unit tests
│   ├── test_base_connector.py
│   ├── test_connector_registry.py
│   ├── test_connector_manager.py
│   ├── test_texas_procurement.py
│   ├── test_agc_texas.py
│   └── test_industry_expansion.py
├── crawlers/                # Crawler unit tests
│   ├── test_http_crawler.py
│   ├── test_html_parser.py
│   └── test_robots.py
├── discovery/               # Discovery engine integration tests
│   ├── test_company_discovery.py
│   ├── test_deduplicator.py
│   ├── test_error_handler.py
│   ├── test_mock_connector.py
│   ├── test_normalizer.py
│   └── ...
└── fixtures/                # Test data (JSON, HTML snapshots)
    ├── txdot_vendor_sample.json
    └── agc_texas_sample.html
```

### Test Categories

| Category | Scope | Mocking Required |
|---|---|---|
| Unit tests | Individual functions/classes | All external deps mocked |
| Contract tests | Interface compliance | None needed |
| Integration tests | Multi-module workflows | External HTTP mocked |
| End-to-end tests | Full request cycle | Minimal mocking |

### Assertion Requirements

For every new feature, tests must verify:
- **Happy path:** Expected output given valid input
- **Boundary values:** Empty strings, max limits, zero results
- **Error handling:** Graceful degradation when dependencies fail
- **Deduplication:** Duplicate entities are collapsed correctly
- **Validation:** Invalid inputs produce expected errors
- **Metrics tracking:** Counts, rates, and summary data are accurate

---

## 7. API Design Standards

### Response Format

All discovery endpoints return consistent structure:

```json
{
  "companies": [
    {
      "company_name": "...",
      "website": "...",
      "city": "...",
      "state": "...",
      "country": "...",
      "source": "...",
      "confidence": 0.85,
      "source_url": "...",
      "discovery_reason": "Matched 'roof' in industry_focus"
    }
  ],
  "metrics": {
    "total_found": 47,
    "total_validated": 42,
    "total_cleaned": 38,
    "errors": ["Connector 'legacy_source' failed: timeout"],
    "timing_ms": 1240
  }
}
```

### Error Responses

| Scenario | HTTP Status | Body |
|---|---|---|
| Missing/invalid query params | 422 | Pydantic validation error |
| All connectors failed | 200 | `{"companies": [], "metrics": {"errors": [...]}}` |
| Partial results | 200 | `{"companies": [...], "metrics": {"partial": true, ...}}` |
| Server crash (unexpected) | 500 | Standard FastAPI error |

**Key rule:** The API always returns 200 for discovery queries. Failure modes are expressed in the response body, not HTTP status codes. This allows clients to handle partial results gracefully.

### Request Validation

All query parameters are validated by Pydantic before reaching the engine:
- `industry`: required, min_length=1, max_length=200
- `location`: required, min_length=1, max_length=200
- `limit`: optional, default=100, ge=1, le=500

Invalid input returns 422 immediately -- no engine execution occurs.

---

## 8. Naming Conventions

| Element | Convention | Example |
|---|---|---|
| Module names | `snake_case` | `company_discovery_engine.py` |
| Class names | `PascalCase` | `CompanyDiscoveryEngine` |
| Function names | `snake_case` | `discover_companies()` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_RETRIES = 3` |
| Private attrs | `_leading_underscore` | `_config` |
| Test files | `test_<module>.py` | `test_company_discovery.py` |
| ADR files | `ADR-NNN-ShortTitle.md` | `ADR-001-Connector-Architecture.md` |
| Archive files | `<original>-archived-YYYY-MM-DD.md` | -- |

---

## 9. Logging Standards

### Logger Setup

```python
import logging
logger = logging.getLogger(__name__)
```

### Log Levels

| Level | Use Case | Example |
|---|---|---|
| `DEBUG` | Trace data, detailed state | Variable values, intermediate results |
| `INFO` | Normal operations | Query started, connector executed, results returned |
| `WARNING` | Recoverable issues | Rate limit hit, robots.txt blocked, missing field |
| `ERROR` | Failures | HTTP 500, connection refused, unhandled exception |

### Forbidden Patterns

```python
# FORBIDDEN
print("Debug info")
print(f"Result: {result}")

# ALLOWED
logger.debug("Debug info")
logger.info("Result: %s", result)
```

---

## 10. Future Engineering Rules

Every sprint must follow this sequence:

```
Architecture Review
       ↓
Implementation
       ↓
Testing
       ↓
Manual QA
       ↓
Documentation Update
       ↓
Sprint Completion
```

Skipping any step is a sprint violation.

---

## 11. Definition of Engineering Done

A feature is complete only if ALL of the following are true:

- [ ] Code works and passes all tests
- [ ] `ruff check .` returns zero violations
- [ ] `black .` formatting is applied
- [ ] Manual QA plan is executed and passes
- [ ] Documentation is updated (architecture, ADR, sprint notes)
- [ ] Architecture is still respected (no new violations introduced)
- [ ] No `print()` statements remain
- [ ] No circular imports exist
- [ ] No TODO comments in production code

If any item is unchecked, the feature is not done. Ship it anyway and you have failed the principle.

---

## 12. Amendments

These principles may be amended only through:

1. **Engineering review** -- documented analysis of why the current principle is insufficient
2. **ADR process** -- formal record of the change with alternatives considered
3. **Founder approval** -- final sign-off required for any amendment

Principles are permanent until deliberately changed. Do not treat them as suggestions.

---

*Last reviewed: 2026-08-03*  
*Next scheduled review: 2027-08-03*
