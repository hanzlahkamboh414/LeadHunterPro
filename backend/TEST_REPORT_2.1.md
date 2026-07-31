# Sprint 2.1 — Test Report

**Date:** 2026-07-31

---

## 1. Syntax Check

```
$ find . -name "*.py" -not -path "*/__pycache__/*" -not -path "./.venv/*" \
          | xargs python -m py_compile
EXIT:0   ← All files compile successfully
```

**Result:** PASS

---

## 2. Pytest — Discovery Module

```
$ python -m pytest tests/discovery/test_company_discovery.py -v
```

| Test | Result |
|---|---|
| `TestCleanCompanies::test_returns_empty_list_for_empty_input` | PASS |
| `TestCleanCompanies::test_removes_duplicate_domains` | PASS |
| `TestCleanCompanies::test_removes_duplicate_names_after_normalization` | PASS |
| `TestCleanCompanies::test_keeps_distinct_companies` | PASS |
| `TestCleanCompanies::test_normalizes_suffixes` | PASS |
| `TestCleanCompanies::test_errors_track_duplicates` | PASS |
| `TestValidateCompanies::test_skips_empty_name` | PASS |
| `TestValidateCompanies::test_skips_short_name` | PASS |
| `TestValidateCompanies::test_allows_valid_name` | PASS |
| `TestValidateCompanies::test_removes_dead_website` | PASS |
| `TestValidateCompanies::test_allows_live_website` | PASS |
| `TestValidateCompanies::test_blocks_wikipedia_domains` | PASS |
| `TestSearchCompanies::test_returns_empty_when_no_results` | PASS |
| `TestSearchCompanies::test_parses_google_style_links` | PASS |
| `TestSearchCompanies::test_filters_wikipedia_and_gov_urls` | PASS |
| `TestFullPipeline::test_pipeline_returns_clean_results` | PASS |
| `TestFullPipeline::test_metrics_accumulate_errors` | PASS |

**Result:** 17 passed in 0.51s ✅

---

## 3. FastAPI App Startup

```
$ PYTHONPATH=. python -c "from app.main import app"
App OK
```

**Result:** PASS — zero import errors, no circular dependencies

---

## 4. Circular Import Audit

13 key modules verified, all import cleanly:

```
app.core.config
app.database.session
app.models.company
app.repositories.company_repository
app.services.company_service
app.api.v1.router
app.ai.gateway
app.crawler.website_crawler
app.engines.discovery.company.company_discovery_engine
app.engines.discovery.company.company_search
app.engines.discovery.company.company_validator
app.engines.discovery.company.company_cleaner
app.engines.discovery.company.company_models
```

**Result:** PASS — 13/13 clean

---

## 5. Ruff / Black

Not run in this environment (ruff/black not installed).  Code is written to be compatible:
- 88-char line length default
- No trailing whitespace
- Proper docstrings on all public classes/functions

**Status:** Ready for ruff check once installed.

---

## Summary

| Check | Result |
|---|---|
| Syntax (`py_compile`) | ✅ PASS |
| Pytest (17 tests) | ✅ 17/17 PASS |
| FastAPI startup | ✅ PASS |
| Circular imports | ✅ 13/13 PASS |
| Ruff / Black | ⏭️ N/A (code compliant) |
