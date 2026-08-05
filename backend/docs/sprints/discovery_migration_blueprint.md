# LeadHunter Pro — Discovery Migration Blueprint

**Status:** APPROVED FOR PLANNING — no code to be written until this document is signed off.
**Objective:** Restore and improve the original API-free discovery pipeline. Demote search providers to an optional amplifier.
**Governing rules:** Reuse existing repo code. Create new abstractions only where a wiring gap genuinely exists. Do not redesign.

> Verification note: every module, signature, and flow below was confirmed by reading the working tree this session. Two items remain **NEEDS GIT VERIFICATION** (the exact deleted `company_search.py` body) and are flagged where relevant.

---

## SECTION 1 — Legacy Discovery Pipeline (as originally designed)

The repo shows the *intended* pre-2.3A flow through surviving modules. `company_search.py` (the seed step) was deleted; its siblings remain and their docstrings describe the contract.

```
User Query (industry, location, limit)
        │
        ▼
[API]  app/api/v1/discovery.py                  → discover_companies()
        │
        ▼
[ENGINE] app/engines/discovery/company/company_discovery_engine.py
        │   docstring: "Never calls search providers directly.
        │   All discovery originates from registered connectors."
        ▼
[SEED]  app/engines/source_intelligence/source_planner.py   → SourcePlanner.plan()
        │   returns ranked public source URLs (trade assocs, licensing
        │   boards, national directories) from source_models.py
        ▼
[SEED DATA] app/engines/source_intelligence/source_models.py
        │   TRADE_ASSOCIATIONS, NATIONAL_SOURCES, LICENSING_BOARDS, NEWS_SOURCES
        ▼
[FETCH] app/crawlers/http_crawler.py            → HTTPCrawler.crawl(CrawlRequest)
        │   async; robots.txt, rate-limit, retry, cache; returns CrawlResponse
        ▼
[PARSE] app/crawlers/html_parser.py             → HTMLParser.parse(html) → ParsedPage
        │   ParsedPage.links enables directory link-following
        ▼
[EXTRACT] app/search_providers/company_extractor.py → CompanyExtractor.extract() → CompanyProfile
        ▼
[CLASSIFY] app/search_providers/contractor_classifier.py → ContractorClassifier.classify()
        │   returns {accepted, trade_category, reject_reason, confidence}
        ▼
[CLEAN]  app/engines/discovery/company/company_cleaner.py    → clean_companies()  (dedup)
        ▼
[VALIDATE] app/engines/discovery/company/company_validator.py → validate_companies()
        ▼
Validated Companies  (+ DiscoveryMetrics)
```

**Modules involved:** `discovery.py`, `company_discovery_engine.py`, `source_planner.py`, `source_models.py`, `http_crawler.py` (+ the 10 supporting `app/crawlers/` modules), `html_parser.py`, `company_extractor.py`, `contractor_classifier.py`, `company_cleaner.py`, `company_validator.py`, `company_models.py`.

**Did it need paid APIs?** No. Zero Google/Bing/DuckDuckGo/SerpAPI code exists anywhere in `backend/app/`. The seed step was `SourcePlanner`, not a search API.

⚠️ **NEEDS GIT VERIFICATION:** the exact role of the deleted `company_search.py` (whether it was the crawl driver or only a thin seed adapter). The plan below does not depend on recovering it — it treats the seed→crawl wiring as *missing* regardless.

---

## SECTION 2 — Current Architecture

```
User Query
        │
        ├─[PATH A] app/api/v1/discovery.py → CompanyDiscoveryEngine → ConnectorManager
        │            → ConnectorRegistry connectors → (fixture data today)
        │
        └─[PATH B] app/api/v1/connectors.py → TexasProcurementConnector.search()
                     → SourceOrchestrator.discover()
                          ├─ SearchProviderSource (priority 50)  → SearchProviderManager
                          │      → SearchProviderRegistry.get_enabled()  ← EMPTY
                          │      → returns EMPTY (no SEARXNG_URL / BRAVE key)
                          └─ FixtureSource (priority 999)  → fixture JSON
```

**Where SearchProviderManager replaced the original pipeline:**
In Sprint 2.3A, the **seed step** (`SourcePlanner → crawl`) was replaced by `SearchProviderSource → SearchProviderManager → SearchProviderRegistry`. The registry is populated only when `SEARXNG_URL` or `BRAVE_SEARCH_API_KEY` is configured (auto-registration in `app/search_providers/__init__.py`).

**Why this made live discovery depend on external providers:**
- With no env var set, `SearchProviderRegistry.get_enabled()` returns `[]`.
- `SearchProviderSource` therefore returns `EMPTY`.
- No other live source is registered, so the orchestrator falls through to `FixtureSource`.
- Result: **discovery only produces non-fixture data when an external API is configured.** The seed step became API-gated, even though `SourcePlanner` + `HTTPCrawler` sit unused in the repo and need no API.

**Root cause (one sentence):** the seed URL step was swapped from a free source planner to an API-gated search registry, and the crawler that consumed seeds was orphaned rather than rewired.

---

## SECTION 3 — Reuse Analysis

### A. Modules to KEEP (unchanged)

| Module | Why |
|--------|-----|
| `app/crawlers/*` (http_crawler, robots, retry, rate_limiter, html_parser, session_manager, cache, config, response, base, exceptions) | Production-grade async crawler with full test suite. It is the fetch engine of the restored pipeline. Currently dead only because nothing calls it. |
| `app/engines/source_intelligence/source_planner.py` | The API-free seed step. Already wired to a live endpoint; produces ranked source URLs. |
| `app/search_providers/company_extractor.py` | HTML→CompanyProfile. Reusable standalone (`extract(url, html, *, title, description, context)`). |
| `app/search_providers/contractor_classifier.py` | Accept/reject + trade classification (`classify(...) -> {accepted, trade_category, reject_reason, confidence}`). Standalone. |
| `app/engines/discovery/company/company_cleaner.py`, `company_validator.py`, `company_models.py` | Dedup + validate + result models. Already the tail of `CompanyDiscoveryEngine`. |
| `app/discovery/source_orchestrator.py` | Multi-source fan-out with status contract + health report. The integration point for the new source. |
| `app/connectors/industry_expansion.py` | Trade keyword expansion. Used across sources. |
| `app/search_providers/*` (manager, registry, searxng, brave, models) | Keep intact — becomes the OPTIONAL amplifier. Founder rule: do not rewrite working infra. |

### B. Modules to MODIFY

| Module | Change | Why |
|--------|--------|-----|
| `app/engines/source_intelligence/source_models.py` | Audit + correct seed URLs (`tdlnr.texas.gov`→`tdlr.texas.gov`, `slbt.ca.gov`, remove "Gambling Commission – not relevant" placeholder) | Seed URLs must be real before they can drive live crawling. Data-only change, no logic. |
| `app/connectors/texas_procurement.py` | Register the new `DirectoryCrawlSource` in its `SourceOrchestrator` at priority < 50 (above search providers) | Makes the crawl path primary and search optional. Additive; no signature change. |
| `app/discovery/sources/__init__.py` | Export new source | Package wiring. |

### C. Modules to REMOVE (dead — delete after path is green)

| Module | Why |
|--------|-----|
| `app/crawler/website_crawler.py` (singular) | Redundant regex crawler; superseded by `app/crawlers/HTTPCrawler`. |
| `app/engines/company_engine.py` | Thin wrapper over the singular crawler; not in any discovery path. |
| `app/services/crawler_service.py` | Duplicate wrapper of the same crawler. |
| `app/research/website/crawler.py`, `scraper.py` | Third crawler generation; unreferenced by discovery. |

> Removal is gated on repointing/removing the standalone `/crawler` and `/research` API endpoints that currently import them (see Phase 5).

### D. Modules to MERGE (consolidate duplicates)

| Modules | Merge into | Why |
|---------|-----------|-----|
| `_parse_location` + state map in `connectors/texas_procurement.py` AND `connectors/connector_manager.py` AND `source_planner._resolve_state` | One shared `app/connectors/location.py` util | Three copies of the same location parsing drift independently. |
| HTML→contact extractors: `search_providers/company_extractor.py` · `crawler/website_crawler.py` · `research/website/parser.py` | Keep `company_extractor.py`; delete the other two | Single extraction implementation. |
| `SourceOrchestrator._deduplicate` AND `ConnectorManager._deduplicate` AND `company_cleaner` | Consolidate on `company_cleaner` (richest: fuzzy + domain + name) | Three dedup strategies produce inconsistent results. |

---

## SECTION 4 — DirectoryCrawlSource Design (design only)

A single new source that bridges the existing seed planner to the existing crawler/extractor/classifier. It is the **only** new abstraction required — justified because no current code connects `SourcePlanner` output to `HTTPCrawler` input.

**Responsibilities**
1. Ask `SourcePlanner` for ranked seed URLs for the query.
2. Crawl each seed URL with `HTTPCrawler` (respecting robots/rate-limit/retry already built in).
3. Parse each page with `HTMLParser`; follow a bounded set of directory/member links (`ParsedPage.links`).
4. Extract a `CompanyProfile` per candidate page via `CompanyExtractor`.
5. Classify via `ContractorClassifier`; drop rejects.
6. Return the discovery status + company dicts + metadata.

**Inputs** — the `BaseSource` contract: `discover(*, industry: str, location: str, limit: int)`.

**Outputs** — the `SourceStatus` 3-tuple already used by the orchestrator:
`(SourceStatus, list[company_dict], metadata_dict)`
- `SUCCESS` — ≥1 classified contractor found
- `EMPTY` — crawled but nothing matched
- `UNAVAILABLE` — all seed hosts unreachable (DNS/timeout) — the graceful-degradation case the founder requires
- `ERROR` — unexpected failure (still never raises to the orchestrator)

**Interaction with the crawler** — constructs `CrawlRequest(url=...)`, awaits `HTTPCrawler.crawl()`. Because the crawler is async and the source contract is sync, the source uses `asyncio.run(...)` at its boundary (identical pattern to the existing `SearchProviderSource`). No crawler change needed.

**Interaction with the extractor** — passes `CrawlResponse` body + `ParsedPage` title/description into `CompanyExtractor.extract(url, html, title=..., description=..., context={industry_hint, location_hint})`. Consumes the returned `CompanyProfile`.

**Interaction with the classifier** — calls `ContractorClassifier.classify(name=..., title=..., description=..., url=..., industry_hint=industry)`; keeps only `accepted is True`; records `trade_category`/`confidence` on the company dict.

**Interaction with the validator** — none directly. Validation/dedup stays where it already lives (`company_cleaner`/`company_validator` in the engine, or `SourceOrchestrator._deduplicate`). The source emits raw-but-classified company dicts; the pipeline validates downstream. This keeps the source single-responsibility.

**Bounded-crawl guardrails (design constraints, not new components):** max seeds per query, max links followed per seed, max total pages, per-host politeness — all configurable via the existing `CrawlerConfig`.

---

## SECTION 5 — Final Architecture

```
User Query (industry, location, limit)
        │
        ▼
SourceOrchestrator.discover()          ← app/discovery/source_orchestrator.py
        │
        ├─ DirectoryCrawlSource     (priority 20)  ← NEW — PRIMARY, API-FREE
        │     SourcePlanner → HTTPCrawler → HTMLParser
        │     → CompanyExtractor → ContractorClassifier
        │
        ├─ SearchProviderSource     (priority 50)  ← OPTIONAL amplifier
        │     only active if SEARXNG_URL / BRAVE key set; else EMPTY (no effect)
        │
        └─ FixtureSource            (priority 999) ← emergency bridge only
        │
        ▼
   aggregate → dedup (company_cleaner) → validate (company_validator) → rank
        │
        ▼
Validated Companies  +  Source Health report
```

**Guarantee:** with **zero** API keys, `DirectoryCrawlSource` runs, crawls real public directories, and returns real companies. Search providers only *add* coverage when configured — they never gate discovery. If the crawl source returns `UNAVAILABLE` (e.g. sandbox DNS block) and no provider is configured, only then does `FixtureSource` activate, with `bridge_mode=true` + explicit `fallback_reason`.

---

## SECTION 6 — Migration Phases

### Phase 1 — Stabilize repository
- **Files:** `tests/discovery/test_source_orchestrator.py`, `tests/discovery/test_discovery_sources.py`, `app/discovery/sources/{base_source,fixture_source,search_provider_source,status}.py`, `source_orchestrator.py`.
- **Goal:** land the in-progress `SourceStatus` 3-tuple contract so the suite is green *before* new work. (No behavior change beyond the contract already begun.)
- **Risks:** contract half-applied across sources/tests → red suite.
- **Rollback:** `git checkout` the four source files + two test files to last green commit (`0964e69`), reverting to the 2-tuple contract.
- **Validation:** `pytest tests/` = 0 failures; `ruff` clean on changed files.

### Phase 2 — Restore original discovery pipeline (no new source yet)
- **Files:** none modified in `app/` except a corrected `source_models.py` (data-only URL fixes). Add characterization tests that call `SourcePlanner`, `HTTPCrawler`, `CompanyExtractor`, `ContractorClassifier` **in isolation** to prove each still works.
- **Goal:** prove every leg of the restored pipeline functions independently, from real (or mocked-HTML) input.
- **Risks:** seed URLs unreachable from CI/sandbox (known DNS limitation) → crawler tests must use mocked HTML, not live network.
- **Rollback:** revert `source_models.py`; delete the new characterization tests.
- **Validation:** unit tests green with mocked HTML fixtures; no live-network dependency in CI.

### Phase 3 — Integrate DirectoryCrawlSource
- **Files:** new `app/discovery/sources/directory_crawl_source.py`; modify `app/discovery/sources/__init__.py`, `app/connectors/texas_procurement.py` (register at priority 20); new `tests/discovery/test_directory_crawl_source.py`.
- **Goal:** the orchestrator runs the crawl source first; with mocked crawler responses it returns classified companies and `data_source="live"`.
- **Risks:** async/sync boundary bugs; unbounded crawl; classifier over-rejecting.
- **Rollback:** unregister the source (one line in `texas_procurement.py`); orchestrator reverts to prior behavior. New files are inert if unregistered.
- **Validation:** integration test with mocked `HTTPCrawler` proves real→extract→classify→output; `UNAVAILABLE` path proven with simulated DNS failure; existing suite still green.

### Phase 4 — Make SearchProvider optional
- **Files:** `app/connectors/texas_procurement.py` (confirm priority ordering: crawl 20 < search 50 < fixture 999). No change to `search_providers/*`.
- **Goal:** demonstrate discovery with **no** env vars returns live crawl results, not fixtures.
- **Risks:** ordering regressions; SearchProviderSource errors bubbling.
- **Rollback:** priorities are data; revert the registration block.
- **Validation:** run with unset `SEARXNG_URL`/`BRAVE_SEARCH_API_KEY` → `data_source="live"`, health report shows `search_providers: EMPTY`, `directory_crawl: SUCCESS`.

### Phase 5 — Remove dead code
- **Files:** delete `app/crawler/website_crawler.py`, `app/engines/company_engine.py`, `app/services/crawler_service.py`, `app/research/website/crawler.py`+`scraper.py`; repoint or remove `app/api/v1/crawler.py` and `app/api/v1/research.py` + their routes in `router.py`; merge duplicate `_parse_location`/dedup per Section 3D.
- **Risks:** an endpoint or test still imports a deleted module → import error at app startup.
- **Rollback:** `git revert` the deletion commit (kept as a single isolated commit).
- **Validation:** `grep` proves zero importers before each delete; app boots (`app.main` imports clean); full suite green.

---

## SECTION 7 — Test Recovery (identify only, do not fix yet)

**Belong to the OLD architecture (evaluate for deletion):**
- `tests/discovery/test_company_discovery.py` — already deleted; confirms the engine-path tests were dropped.
- Any `test_crawler.py` at repo root exercising the singular `WebsiteCrawler`.

**Should be UPDATED (contract drift, Phase 1):**
- `tests/discovery/test_source_orchestrator.py` — `FakeSource` + assertions to the `SourceStatus` 3-tuple.
- `tests/discovery/test_discovery_sources.py` — `FixtureSource`/`SearchProviderSource` unpack 3-tuple.
- `tests/connectors/test_live_pipeline.py` — `test_search_prefers_live_over_fixture` expectations under the new priority order.

**Should be DELETED (superseded):**
- Tests targeting `company_engine`/`crawler_service`/`research.website.crawler` once those modules are removed in Phase 5.

**NEW tests required:**
- `test_directory_crawl_source.py` — mocked `HTTPCrawler` → extract → classify → output; and the `UNAVAILABLE`/`EMPTY`/`ERROR` status paths.
- `test_source_planner_urls.py` — assert corrected seed URLs are well-formed (Phase 2).
- End-to-end orchestrator test: no API keys → `data_source="live"` from crawl source.
- Location-util consolidation test (Section 3D merge).

---

## SECTION 8 — Final Review (self-critique)

**Assumptions challenged:**
1. *"SourcePlanner URLs are usable seeds."* — Partly false. Several are placeholders/typos, and some point at JS-heavy or login-walled sites (AGC domain was for-sale in earlier research; county portals need registration). **Mitigation:** Phase 2 URL audit is mandatory, and the design treats unreachable seeds as `UNAVAILABLE`, not failure.
2. *"HTTPCrawler works end-to-end."* — Verified structurally (async, aiohttp, robots/retry/cache) but **never exercised against these seeds**. It's well-tested in isolation; integration is unproven. **Mitigation:** Phase 2 proves each leg with mocked HTML before Phase 3 wires them.
3. *"Crawling directories yields company records."* — Directory pages list members but layouts vary wildly; one generic extractor will not parse every directory. **Hidden risk:** extraction quality per-site. **Mitigation:** start with 2–3 high-value, stable sources; treat per-site parsing as incremental, not all-at-once.
4. *"Sandbox can validate live discovery."* — False. This environment blocks `.gov`/state DNS (`mycpa.cpa.state.tx.us`, `texas.gov` fail; only `txsmartbuy.gov`/`google.com` resolve). **All CI validation must use mocked HTML.** Live validation happens only on the deployment host.

**Hidden risks:**
- **Legal/politeness:** crawling must honor robots.txt (already built) and reasonable rates; some directories prohibit scraping in ToS — needs a per-source allowlist decision.
- **JS-rendered directories** return empty HTML to a non-browser crawler (proven with `txsmartbuy.gov`). Those sources are out of scope until/unless a headless renderer is added — explicitly *not* in this plan.
- **Async/sync boundary:** `asyncio.run()` inside a sync source is fine standalone but will raise if ever called from an already-running event loop (e.g. inside FastAPI async route). The engine path is sync today; keep it that way or the source needs an async-aware entry.

**Recommended simplifications:**
1. **Do not build a generic "crawl any directory" engine.** Scope `DirectoryCrawlSource` to a small, curated, verified seed set first. Breadth is a later increment.
2. **Do not consolidate dedup/location utils in the migration path (Phase 5).** It's cleanup, not restoration — defer to avoid coupling risk into the core work. Ship discovery first; refactor duplicates after.
3. **Keep validation where it already lives.** Do not add validation logic to the new source; reuse `company_cleaner`/`company_validator` unchanged.
4. **One new file only** (`directory_crawl_source.py`) plus data fixes and registrations. If the plan ever grows a second new abstraction, stop and re-review — that's the redesign the founder forbade.

**Verdict on the plan:** sound and minimal — it restores existing parts with a single bridge component. The biggest real risk is not architecture but **seed-URL/extraction quality**, which is empirical and must be proven per-source against the live deployment network, not in this sandbox.

---

## SECTION 9 — Final Architectural Verification (pre-Phase-1)

Independent re-review against the working tree. Every module in the blueprint was opened and its interface confirmed. **Two findings the earlier draft missed** are recorded here and change the plan.

### 9.1 Interface verification — all confirmed

| Module | Interface claim | Result |
|--------|-----------------|--------|
| `crawlers/base.py` | `CrawlRequest(url, method, headers, timeout, follow_redirects, respect_robots, session_id)`; `BaseCrawler.crawl()` + **`close()`** both abstract async | VERIFIED |
| `crawlers/http_crawler.py` | `HTTPCrawler.crawl(CrawlRequest) -> CrawlResponse`, async, aiohttp, `.parser` property | VERIFIED |
| `crawlers/response.py` | `CrawlResponse` frozen dataclass; `.content: bytes`, **`.text` property** (utf-8 decode w/ latin-1 fallback), `.successful`, `.error` | VERIFIED |
| `crawlers/html_parser.py` | `HTMLParser.parse(html, base_url="") -> ParsedPage`; `ParsedPage.links: list[str]`, `.social_links` | VERIFIED |
| `source_intelligence/source_planner.py` | `SourcePlanner.plan(SourcePlannerRequest) -> SourcePlannerResult` | VERIFIED |
| `source_intelligence/source_models.py` | `SourceRecord(name, url, priority, crawl_strategy, supports_company_discovery, ...)`; `SourcePlannerRequest(industry, country, state, city)` | VERIFIED |
| `search_providers/company_extractor.py` | `CompanyExtractor.extract(url, html, *, title="", description="", context=None) -> CompanyProfile` | VERIFIED |
| `search_providers/company_extractor.py` | `CompanyProfile` fields: `name, website, city, state, country, trade_category, industry_focus, phone, email, address, source_url, confidence, rejected_reason`; props `is_valid`, `is_rejected` | VERIFIED |
| `search_providers/contractor_classifier.py` | `classify(*, name, title, description, url, industry_hint) -> {accepted, trade_category, reject_reason, confidence}` | VERIFIED |
| `engines/discovery/company/company_cleaner.py` | `clean_companies(list[CompanyDiscoveryResult]) -> (list, DiscoveryMetrics)` | VERIFIED |
| `engines/discovery/company/company_validator.py` | `validate_companies(list[CompanyDiscoveryResult], *, timeout=10) -> (list, DiscoveryMetrics)` | VERIFIED |
| `discovery/source_orchestrator.py` | `discover()` unpacks `(SourceStatus, list, dict)` 3-tuple | VERIFIED |
| `discovery/sources/status.py` | `SourceStatus.{SUCCESS, EMPTY, UNAVAILABLE, ERROR}` | VERIFIED |

### 9.2 Circular-dependency analysis — CLEAR (with one caveat)

Grep for upward imports proved the dependency graph is a clean DAG:
- `app/crawlers/*` — imports **nothing** from discovery/search_providers/engines/connectors (leaf, as its docstring claims).
- `app/search_providers/*` — imports **nothing** from discovery/engines/connectors.
- `app/engines/source_intelligence/*` — imports **nothing** from discovery/crawlers/search_providers.

Therefore `DirectoryCrawlSource` (in `app/discovery/sources/`) may safely import `source_planner`, `HTTPCrawler`, `CompanyExtractor`, `ContractorClassifier` — **no cycle introduced.**

**FINDING 1 (missed by draft) — existing latent cycle in discovery <-> connectors.**
`app/discovery/sources/fixture_source.py:99` and `search_provider_source.py:80` do `from app.connectors.texas_procurement import _parse_location`, while `app/connectors/texas_procurement.py:147-149` does `from app.discovery... import SourceOrchestrator/FixtureSource/SearchProviderSource`. This is a **real bidirectional dependency** between `discovery` and `connectors`. It works today **only because both sides use function-local (deferred) imports**, not module-top imports. **Consequence:** `DirectoryCrawlSource` must likewise import `_parse_location` *inside* its method OR — better — Section 3D's `location.py` util must be extracted **before** the source is written. **This promotes the location-util extraction from Phase 5 cleanup to a Phase 3 prerequisite.**

### 9.3 Phase 1 safety — CONFIRMED, with a scope correction

Phase 1 (land the `SourceStatus` 3-tuple contract) touches only `app/discovery/sources/*` + orchestrator + their tests. Verified blast radius:
- The only production caller of the orchestrator is `TexasProcurementConnector.search()` (`texas_procurement.py:147-158`), which consumes the orchestrator's `(companies, metadata)` return — unaffected by the internal source-tuple change.
- `CompanyDiscoveryEngine` (Path A) does **not** touch the orchestrator or these sources — it goes through `ConnectorManager`. Phase 1 cannot break Path A.
- No module outside `app/discovery/` imports these sources except through the orchestrator.

**FINDING 2 (missed by draft) — Phase 1 is already partially applied and the working tree is RED.**
The production sources already return/consume the 3-tuple, but some **tests** were left mid-edit. Phase 1 is therefore *not* "introduce the contract" — it is **"finish applying the contract to the remaining tests and reach green."** Rollback target `0964e69` predates the contract entirely; reverting would discard correct production changes. **Corrected Phase 1 rollback:** roll forward (finish the test edits) or `git stash` only the test files — do **not** revert the source files.

### 9.4 Additional verified risk — `company_validator` makes live network calls

`validate_companies()` calls `_is_live_website()` which issues real HTTP HEAD requests (`company_validator.py:59`). In this sandbox (and any offline CI) **every company fails validation** because most hosts don't resolve. **Consequence:** the "no API keys -> live companies" validation (Phase 4) **cannot pass in CI** if routed through `company_validator`. Tests must either mock `_is_live_website` or assert on pre-validation orchestrator output.

### 9.5 Assumption ledger

| # | Assumption | Status |
|---|-----------|--------|
| 1 | All 13 blueprint modules exist with described interfaces | **VERIFIED** |
| 2 | `CrawlResponse.text` yields HTML for the extractor | **VERIFIED** |
| 3 | `HTMLParser.ParsedPage.links` enables directory link-following | **VERIFIED** |
| 4 | No circular dep introduced by DirectoryCrawlSource | **VERIFIED** (via deferred/extracted import — see 9.2) |
| 5 | Phase 1 cannot break Path A (engine) or Path B (connector) | **VERIFIED** |
| 6 | `HTTPCrawler` is async + a context manager -> source needs `async with` + `asyncio.run` | **VERIFIED** |
| 7 | `SourcePlanner` seed URLs are reachable & correct | **UNKNOWN** — several typos confirmed; live reachability unproven (sandbox DNS block) |
| 8 | A single generic extractor parses real directory HTML into companies | **UNKNOWN** — per-site layout variance unproven; needs live HTML samples |
| 9 | Deployment host can reach `.gov`/state directories | **UNKNOWN** — sandbox cannot; deployment network untested |
| 10 | Exact behavior of deleted `company_search.py` | **UNKNOWN** — git classifier unavailable; plan does not depend on it |
| 11 | `company_validator` live-HEAD works in CI | **VERIFIED FALSE** — must be mocked (see 9.4) |
| 12 | robots.txt / ToS permit crawling target directories | **LIKELY** for public trade-assoc/gov listings; **UNKNOWN** per specific site |

### 9.6 Blueprint changes made as a result

1. **Section 3D partially promoted to Phase 3 prerequisite:** extract shared `location.py` (`_parse_location`) **before** writing `DirectoryCrawlSource` (Finding 1).
2. **Phase 1 rollback corrected:** roll forward / stash tests only — do **not** revert to `0964e69` (Finding 2).
3. **Phase 4 validation corrected:** assert on orchestrator output or mock `_is_live_website`; live validation is deployment-host-only (9.4).
4. **DirectoryCrawlSource design note:** must use `async with HTTPCrawler(...)` and reach `_parse_location` via a function-local import (or the new `location.py`) to preserve the acyclic graph.

**Final go/no-go:** Blueprint is architecturally sound and safe to implement. No blocker. The two missed findings are wiring-discipline items (deferred imports, corrected rollback), not design flaws. Proceed to Phase 1 with the corrected rollback strategy.

---

*End of blueprint. No repository files were modified in producing this verification (documentation only).*
