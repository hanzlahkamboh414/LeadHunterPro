# Sprint 2.1 Report — Company Discovery Engine

**Status:** VERIFIED ✅
**Date:** 2026-07-31
**Owner:** Hanzlah

---

## Objective Achieved

Built the first production-ready Company Discovery Engine that searches public sources (Google, Bing) for construction estimating companies, validates their websites, removes duplicates, and returns clean structured lead candidates — **with zero AI involvement**.

---

## Files Created

| File | Responsibility |
|---|---|
| `app/engines/discovery/company/company_models.py` | Immutable data models (`CompanyDiscoveryResult`, `DiscoveryMetrics`) |
| `app/engines/discovery/company/company_search.py` | Public-source search (Google + Bing SERP parsing) |
| `app/engines/discovery/company/company_validator.py` | Website liveness check, name validation, blocked-domain filtering |
| `app/engines/discovery/company/company_cleaner.py` | Deduplication by domain + normalized name, corporate-suffix stripping |
| `app/engines/discovery/company/company_discovery_engine.py` | Main orchestrator — Search → Validate → Clean → Return |
| `app/engines/discovery/company/README.md` | Module documentation |
| `tests/discovery/test_company_discovery.py` | 17 unit/integration tests |

---

## Files Modified

None. No existing code was touched — the discovery engine is fully additive.

---

## Summary

The discovery engine follows a strict three-stage pipeline:

1. **Search** — Queries Google & Bing for the given industry + location, extracts company names and URLs from SERP results, filters out `.gov` and Wikipedia domains.
2. **Validate** — HEAD-checks each website for liveness, rejects empty/short names, blocks non-commercial domains.
3. **Clean** — Deduplicates by normalized domain and company name (suffix-stripped), tracks all removed entries in metrics.

The engine exposes a single public method:

```python
engine = CompanyDiscoveryEngine()
results, metrics = engine.discover(
    industry="Construction Estimating",
    location="Dallas Texas USA",
    limit=100,
)
```

Returns `list[CompanyDiscoveryResult]` with fields: `company_name`, `website`, `city`, `state`, `country`, `source`, `confidence`.

---

## Acceptance Criteria Status

| Criterion | Status |
|---|---|
| Engine returns clean companies | ✅ |
| No duplicate companies | ✅ (domain + name dedup) |
| No duplicate domains | ✅ |
| Website validation works | ✅ (HEAD request + exception handling) |
| All tests pass | ✅ 17/17 |
| No architecture violations | ✅ (engines/ layer only, no DB/AI) |
| No circular imports | ✅ 13/13 modules import cleanly |
| No AI usage | ✅ Pure HTTP + regex |

---

## Known Limitations (Future Sprints)

- Search uses direct HTTP requests (not a dedicated search API); accuracy improves with proxy rotation or API keys.
- No geocoding validation for city/state extraction from SERP snippets yet.
- `max_pages=3` limits result depth; tunable per call.
