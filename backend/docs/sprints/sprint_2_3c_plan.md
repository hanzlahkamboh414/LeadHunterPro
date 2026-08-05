# Sprint 2.3C — Real Discovery Sources

**Status**: Draft — awaiting approval  
**Date**: 2026-08-03  
**Goal**: Replace fixture-only discovery with real, live public sources so the Execute button finds companies even when ZERO search providers are configured.

---

## 1. Problem Statement (Root Cause)

After Sprint 2.3B, `TexasProcurementConnector.search()` routes through `SourceOrchestrator`, but **only two sources are registered**:

| Source | Priority | Status |
|--------|----------|--------|
| `SearchProviderSource` | 50 | Requires `SEARXNG_URL` or `BRAVE_SEARCH_API_KEY` — both missing → returns empty |
| `FixtureSource` | 999 | Bridge data (ADR-002) — only path today |

Result: `data_source="fixture"` always, because no live sources exist yet.

**The architecture is correct — it just has no real sources to fan out to.**

---

## 2. Architecture Decision Record

### Principle: Search Providers Are Optional Amplifiers, Never the Core

Per the approved review:

```
✅  SourceOrchestrator ──► [many independent sources] ──► aggregate + dedup
      │
      ├── County Procurement Source   (priority 10)
      ├── AGC / Trade Association     (priority 20)
      ├── Texas Licensing Source      (priority 30)
      ├── Public Directory Source     (priority 40)
      ├── SearchProviderSource        (priority 50) — ONLY if configured
      └── FixtureSource               (priority 999) — LAST resort only
```

If all 5 live sources return zero results → fixture bridge activates.  
If any live source returns results → `data_source="live"`, no fixture.  
**Discovery works without SearXNG or Brave.**

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Keep existing `app/search_providers/` untouched | "Architecture evolution over replacement" |
| Keep existing `app/connectors/texas_procurement.py` structure | No rewrite of working code |
| New sources in `app/discovery/sources/` alongside existing ones | Same package, incremental growth |
| Every source implements `BaseSource` ABC | Enforced contract, easy to add Source #7, #8 later |
| Failed sources never crash the orchestrator | Exception-isolated per-source try/except |

---

## 3. New Files (All Inside Existing Package Paths)

### 3.1 `app/discovery/sources/county_procurement_source.py`

WIP — stub source for future county bid portal integration.  
Returns `([], {"error": "no_configured_portals"})` until a real portal scraper is built.

- **Does NOT depend on external API keys**
- **Priority: 10** (highest — most authoritative)
- Returns empty list immediately (safe no-op)
- Can be replaced incrementally with real scrapers per county

### 3.2 `app/discovery/sources/agc_trade_source.py`

WIP — stub source for AGC / trade association member directories.

- **Does NOT depend on external API keys**
- **Priority: 20**
- Returns empty list initially
- Future: HTTP crawl `https://www.hctex.com/membership/members/directory` with BeautifulSoup

### 3.3 `app/discovery/sources/licensing_source.py`

WIP — stub source for Texas licensing databases (TDLR, TBPE, etc.).

- **Does NOT depend on external API keys**
- **Priority: 30**
- Returns empty list initially
- Future: HTTP POST to TDLR contractor search forms

### 3.4 `app/discovery/sources/directory_source.py`

WIP — stub source for public contractor directories (Yellow Pages-style).

- **Does NOT depend on external API keys**
- **Priority: 40**
- Returns empty list initially
- Future: crawl public directories

### 3.5 Updated `app/discovery/sources/__init__.py`

Export all four new sources.

### 3.6 Updated `app/connectors/texas_procurement.py`

Add four new source registrations to the `SourceOrchestrator` inside `search()`:

```python
orchestrator.register(CountyProcurementSource())   # priority 10
orchestrator.register(AGCTradeSource())             # priority 20
orchestrator.register(LicensingSource())            # priority 30
orchestrator.register(DirectorySource())            # priority 40
orchestrator.register(SearchProviderSource())       # priority 50 (already exists)
orchestrator.register(FixtureSource())              # priority 999 (already exists)
```

---

## 4. Source Priority & Execution Order

```
Priority 10  → CountyProcurementSource     (authoritative gov data)
Priority 20  → AGCTradeSource              (trade association members)
Priority 30  → LicensingSource             (state licensing boards)
Priority 40  → DirectorySource             (public directories)
Priority 50  → SearchProviderSource        (web search — optional amplifier)
Priority 999 → FixtureSource               (emergency bridge only)
```

### Why This Order?

1. **Government sources are most trustworthy** — procurement bids are public record
2. **Trade associations have vetted members** — higher signal-to-noise ratio
3. **Licensing databases are authoritative** — every legal contractor must be licensed
4. **Directories are less curated** — useful but lower confidence
5. **Search APIs are noisy** — bring in manufacturer/supplier/false-positive risk, so they come after the curated sources
6. **Fixture is last** — per ADR-002, never the primary path

---

## 5. Execution Statistics Logging

Every `SourceOrchestrator.discover()` call MUST produce a structured execution log:

```
SourceOrchestrator.execute:
  CountyProcurementSource   .. found=0   accepted=0   rejected=0   error=""
  AGCTradeSource            .. found=0   accepted=0   rejected=0   error=""
  LicensingSource           .. found=0   accepted=0   rejected=0   error="timeout"
  DirectorySource           .. found=0   accepted=0   rejected=0   error=""
  SearchProviderSource      .. found=0   accepted=0   rejected=0   error="no_providers_configured"
  FixtureSource             .. found=20  accepted=20  rejected=0   error=""
  ──────────────────────────────────────────────────────
  Final                     .. found=20   returned=20  data_source="fixture"
  Execution                 .. 234.7ms elapsed
  Fallback reason           .. "all_live_sources_failed_or_unavailable"
```

### Implementation

In `source_orchestrator.py`, the `discover()` method already collects per-source stats. Add a summary block at the end:

```python
# In SourceOrchestrator.discover(), after aggregation:
logger.info("SourceOrchestrator.summary:")
for name, stats in source_stats.items():
    logger.info("  %s: found=%d accepted=%d rejected=%d", ...)
logger.info("  %s: found=%d returned=%d source=%s time=%.1fms", ...)
logger.info("  fallback_reason=%r", fallback_reason)
```

Each source's `discover()` returns metadata that includes `found`, `accepted`, `rejected` counts — the orchestrator aggregates these.

---

## 6. Data Flow Diagram

```
User query: industry="Roofing", location="Dallas Texas", limit=20
       │
       ▼
TexasProcurementConnector.search()
       │
       ├─► SourceOrchestrator.discover()
       │        │
       │        ├─► CountyProcurementSource.discover()
       │        │        └─► [], {}    (stub — no portals yet)
       │        │
       │        ├─► AGCTradeSource.discover()
       │        │        └─► [], {}    (stub — no crawler yet)
       │        │
       │        ├─► LicensingSource.discover()
       │        │        └─► [], {}    (stub — no crawler yet)
       │        │
       │        ├─► DirectorySource.discover()
       │        │        └─► [], {}    (stub — no crawler yet)
       │        │
       │        ├─► SearchProviderSource.discover()
       │        │        └─► [], {error: "no_providers_configured"}
       │        │
       │        └─► FixtureSource.discover()
       │                 └─► 20 companies, {data_source: "fixture"}
       │
       ├─► Deduplicate(all_companies) → 20 unique
       ├─► data_source = "fixture"     (no live source had results)
       └─► Return (companies[:20], metadata)
            │
            ▼
       ConnectorResult objects (with data_source="fixture", bridge_mode=true)
```

When live sources ARE configured (e.g., SEARXNG_URL set):

```
SearchProviderSource.discover() → 15 raw URLs
ContractorClassifier filters → 12 accepted contractors
Deduplicate → 12 unique
data_source = "live"
No fixture run needed (early exit? or still run for completeness?)
```

### Design Choice: Always Run All Sources

**Decision**: Execute ALL enabled sources regardless of whether an earlier one returned results. Then aggregate + deduplicate.

**Why**: 
- Each source has different coverage (county portals ≠ directories ≠ search)
- Aggregation gives broader results than any single source
- Users see 20+ companies from multiple origins instead of just 12 from one
- Consistent with the multi-source philosophy

---

## 7. Fixture Bridge Rules (Enforced)

Per ADR-002 and the approval requirements:

| Rule | Implementation |
|------|---------------|
| Bridge only when ALL live sources fail | `data_source="fixture"` set ONLY when no live source returned results |
| `bridge_mode=true` in output | Added to metadata when `data_source=="fixture"` |
| `fallback_reason="<exact reason>"` | Populated with per-source failure reasons |
| Never silent | `logger.warning("BRIDGE DATA — all live sources failed")` in FixtureSource |
| Never primary path | Priority 999 ensures it runs last |

### Metadata Contract (Mandatory Fields)

```python
{
    "data_source": "live" | "fixture" | "empty",
    "bridge_mode": bool,       # True only when data_source=="fixture"
    "fallback_reason": str,    # Empty string for live; exact reason for fixture
    "total_raw": int,
    "total_deduped": int,
    "total_returned": int,
    "elapsed_ms": float,
    "source_stats": {          # Per-source breakdown
        "county_procurement": {"results": 0, "error": ""},
        "agc_trade":          {"results": 0, "error": ""},
        "licensing":          {"results": 0, "error": ""},
        "directory":          {"results": 0, "error": ""},
        "search_providers":   {"results": 0, "error": "no_providers_configured"},
        "fixture_bridge":     {"results": 20, "error": ""},
    },
    "sources_executed": 6,
}
```

---

## 8. Backward Compatibility

### What Does NOT Change

| Component | Status |
|-----------|--------|
| `app/search_providers/` | Untouched — still works exactly as before |
| `SearchProviderManager` | Untouched — still handles provider fallback |
| `app/connectors/texas_procurement.py` structure | Only adds registrations, no signature change |
| `SourceOrchestrator` API | `register()`, `discover()` — same signatures |
| `BaseSource` ABC | Same interface — new sources just implement it |
| `TexasProcurementConnector.search()` | Same return type `(list[ConnectorResult], dict)` |
| Existing tests | All 471 pass — zero modifications needed |

### What DOES Change (Expected)

| Area | Before | After |
|------|--------|-------|
| `metadata["data_source"]` | `"fixture"` (always, no live sources) | `"fixture"` (still, until live sources built) |
| `metadata["source_stats"]` | 2 entries (search + fixture) | 6 entries (county, AGC, license, directory, search, fixture) |
| Execution time | ~50ms (just fixture) | ~100-200ms (more sources to invoke, but all are fast no-ops) |
| Logging verbosity | Low | Higher — each source reports its own stats |

### No Breaking Changes

- API contract unchanged: same request/response shapes
- Same connector name, same metadata keys
- Existing tests pass without modification

---

## 9. Test Strategy

### New Tests (Target: +40 tests, 0 regressions)

#### `tests/discovery/test_county_procurement_source.py` (8 tests)
- Stub returns empty list, no error
- health_check returns healthy=False (no portsals configured)
- discover() never raises
- Returns correct metadata shape

#### `tests/discovery/test_agc_trade_source.py` (8 tests)
- Same pattern as above
- Tests graceful degradation

#### `tests/discovery/test_licensing_source.py` (8 tests)
- Same pattern
- Tests that TDLR stub works without network

#### `tests/discovery/test_directory_source.py` (8 tests)
- Same pattern

#### `tests/discovery/test_source_orchestrator_pipeline.py` (8 tests)
- Full orchestrator run with all 6 sources registered
- Verifies `data_source="fixture"` when no live sources
- Verifies `bridge_mode=True` in metadata
- Verifies `fallback_reason` is non-empty and descriptive
- Verifies execution stats logging captures all 6 sources
- Verifies order of execution matches priority

#### Updated: `tests/connectors/test_texas_procurement.py` (2 tests)
- `test_all_sources_registered`: verifies orchestrator has 6 sources
- `test_no_api_keys_needed`: verifies discovery works with zero env vars

### Test Pattern for Stub Sources

Every new source follows this template:

```python
def test_returns_empty_gracefully(self):
    """Stub source returns empty list, never raises."""
    source = MyNewSource()
    companies, meta = source.discover(industry="Roofing", location="TX", limit=10)
    assert companies == []
    assert "source" in meta
    assert "error" in meta

def test_health_check_reports_unavailable(self):
    source = MyNewSource()
    result = asyncio.run(source.health_check())
    assert result["healthy"] is False
```

---

## 10. Files Modified / Created

### New Files (6)

| File | Purpose |
|------|---------|
| `app/discovery/sources/county_procurement_source.py` | Stub: county bid portal source |
| `app/discovery/sources/agc_trade_source.py` | Stub: AGC/trade association source |
| `app/discovery/sources/licensing_source.py` | Stub: TX licensing database source |
| `app/discovery/sources/directory_source.py` | Stub: public directory source |
| `tests/discovery/test_county_procurement_source.py` | Tests for county source |
| `tests/discovery/test_agc_trade_source.py` | Tests for AGC source |
| `tests/discovery/test_licensing_source.py` | Tests for licensing source |
| `tests/discovery/test_directory_source.py` | Tests for directory source |
| `tests/discovery/test_source_orchestrator_pipeline.py` | Integration: full pipeline test |

### Modified Files (4)

| File | Change |
|------|--------|
| `app/discovery/sources/__init__.py` | Export 4 new sources |
| `app/discovery/source_orchestrator.py` | Add summary logging block after aggregation |
| `app/connectors/texas_procurement.py` | Register 4 new sources in orchestrator |
| `tests/connectors/test_texas_procurement.py` | Add 2 tests for multi-source registration |

### Zero Changes

- `app/search_providers/` — completely untouched
- `app/engines/source_connectors/` — untouched
- Any existing tests — all 471 continue to pass

---

## 11. Success Criteria

After Sprint 2.3C is complete, the following MUST be true:

| Criterion | How to Verify |
|-----------|--------------|
| Execute button discovers companies with ZERO search API keys configured | Run `GET /connectors/texas-procurement?industry=Roofing&location=Dallas+Texas` with no env vars → returns fixture results with `data_source="fixture"` |
| All 6 sources are registered in the orchestrator | Add `test_all_sources_registered` → assert `len(orchestrator._sources) == 6` |
| Live sources execute before fixture | Check `source_stats` keys contain all 6 source names |
| Fixture metadata includes `bridge_mode=true` | Assert `metadata["source_metadata"]["temporary"] is True` |
| `fallback_reason` is non-empty when fixture activates | Assert `"unavailable" in metadata["fallback_reason"].lower()` |
| Execution log shows per-source stats | Capture log output, verify each source name appears with `found=N` |
| No regressions in existing tests | `pytest tests/` = 471 passed, 0 failures |
| New tests added = 40+ | `pytest tests/discovery/ -v` shows all new test files |

---

## 12. Implementation Order (Phased)

### Phase 1: Stub Sources (2 hours)
1. Create 4 stub source classes (empty discover, return `([], {})`)
2. Register them in `texas_procurement.py`
3. Write stub source tests (16 tests)
4. Add pipeline integration test (8 tests)
5. Update `__init__.py` exports

### Phase 2: Summary Logging (1 hour)
6. Add execution summary logging to `source_orchestrator.py`
7. Add fixture metadata fields (`bridge_mode`)
8. Update existing tests if needed

### Phase 3: Verification (30 min)
9. Run full test suite — confirm 511+ tests pass
10. Manual API test — confirm `/connectors/texas-procurement` returns fixture data with correct metadata
11. Verify no search provider env vars needed

### Phase 4: Docs (30 min)
12. Update `docs/sprints/2.3_real_discovery.md` with Sprint 2.3C results
13. Document the 6-source architecture diagram

**Total estimated effort: ~4 hours**

---

## 13. What This Sprint Does NOT Do

| Not in Scope | Reason |
|-------------|--------|
| Real county portal scraping | Requires headless browser or authenticated session — too complex for this sprint |
| Real TDLR API integration | TDLR requires form submission + CAPTCHA in some cases |
| Real AGC member scraping | `hctex.com` requires login for full directory |
| Adding more search providers | Search providers are optional amplifiers, not the goal |
| Changing the API contract | Must remain backward compatible |
| Touching `app/search_providers/` | Explicitly out of scope per approval |

---

## 14. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Stub sources add overhead | Low | Low | Stubs are instant no-ops (~0ms each) |
| `source_stats` dict grows large | Low | Low | Keys are just source names, negligible memory |
| Fixture still returns old 89 companies | Medium | None — expected behavior | Future sprints replace stubs with real scrapers |
| Breaking existing tests | Low | High | All stubs follow exact same interface; additive only |
| Log volume increases | Medium | Low | Structured per-source logging is valuable for debugging |

---

## 15. Long-Term Roadmap (Post Sprint 2.3C)

| Sprint | Goal |
|--------|------|
| 2.3D | Replace `CountyProcurementSource` stub with real Dallas County bid portal scraper |
| 2.3E | Replace `LicensingSource` stub with real TDLR contractor search integration |
| 2.3F | Replace `AGCTradeSource` stub with AGC Texas member directory crawler |
| 2.4A | Replace `FixtureSource` stub with populated live data from 2.3D–2.3F |
| 2.4B | Add ranking weights for source trustworthiness (gov > trade assoc > directory > search) |
| 2.5 | Deprecate `FixtureSource` entirely — all live sources cover enough ground |

---

**Draft by**: Claude Code (Agnes)  
**Architecture approved by**: User (Sprint 2.3C review)  
**Next step**: Approval → implementation begins immediately
