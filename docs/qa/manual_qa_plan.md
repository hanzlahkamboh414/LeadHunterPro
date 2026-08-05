# Manual QA Plan — Discovery Pipeline

**Date:** 2026-08-03  
**Sprint:** 2.2 (Real Discovery Foundation)  
**Status:** PLANNED

---

## Purpose

This document defines the manual QA procedures for validating the Discovery Engine after each sprint milestone. It replaces ad-hoc testing with structured, repeatable verification.

---

## Pre-Flight Checks

Before running any discovery queries, verify:

| Check | Command | Expected |
|---|---|---|
| Server starts | `python -m uvicorn app.main:app --reload` | No import errors, UVicorn ready |
| Swagger loads | Open http://127.0.0.1:8000/docs | All endpoints listed |
| Database connects | Health endpoint | 200 OK |
| No print() leakage | Check terminal output | Only structured log lines |

---

## Test Suite

### Test Group 1 — Trade-Specific Queries

These are the critical failures reported in Sprint 2.1R manual QA. They must pass.

| # | Query | Endpoint | Pass Criteria |
|---|---|---|---|
| T1.1 | `industry=Roofing&location=Dallas Texas&limit=20` | `/api/v1/discovery/companies` | ≥5 companies, each with non-empty `company_name`, `website`, `city`, `state` |
| T1.2 | `industry=Plumbing&location=Houston Texas&limit=20` | `/api/v1/discovery/companies` | ≥5 companies |
| T1.3 | `industry=Electrical&location=Austin Texas&limit=20` | `/api/v1/discovery/companies` | ≥5 companies |
| T1.4 | `industry=HVAC&location=San Antonio Texas&limit=10` | `/api/v1/discovery/companies` | ≥3 companies |
| T1.5 | `industry=Concrete&location=Fort Worth Texas&limit=10` | `/api/v1/discovery/companies` | ≥3 companies |

### Test Group 2 — Limit Enforcement

Verifies that `limit` parameter is respected and not silently capped by dataset size.

| # | Query | Endpoint | Pass Criteria |
|---|---|---|---|
| T2.1 | `industry=General Contractor&location=TX&limit=50` | `/api/v1/discovery/companies` | ≤50 results returned |
| T2.2 | `industry=Commercial Construction&location=TX&limit=100` | `/api/v1/discovery/companies` | ≤100 results returned |
| T2.3 | `industry=Residential&location=TX&limit=5` | `/api/v1/discovery/companies` | ≤5 results returned |
| T2.4 | `industry=General Contractor&location=TX&limit=1` | `/api/v1/discovery/companies` | Exactly 1 result OR `[]` if no match |

### Test Group 3 — Deduplication

Verifies no duplicate domains appear in results.

| # | Query | Pass Criteria |
|---|---|---|
| T3.1 | Any broad query with limit=50 | No two results share the same normalized domain |
| T3.2 | Same query run twice | Identical result set (deterministic fixtures) |

### Test Group 4 — Connector Metadata

Verifies connectors report their data source correctly.

| # | Endpoint | Pass Criteria |
|---|---|---|
| T4.1 | `GET /api/v1/connectors/` | Lists all registered connectors with names and descriptions |
| T4.2 | `GET /api/v1/connectors/texas-procurement` | Returns metadata including dataset size |
| T4.3 | `GET /api/v1/connectors/agc-texas` | Returns metadata including member count |

### Test Group 5 — Edge Cases

| # | Query | Pass Criteria |
|---|---|---|
| T5.1 | `industry=NonExistentTrade&location=ZZ&limit=10` | Returns `[]` with clear metadata, NOT a server error |
| T5.2 | `industry=&location=Dallas Texas&limit=10` | Returns 422 (industry is required) |
| T5.3 | `industry=Roofing&location=Dallas Texas&limit=0` | Returns 422 (limit must be ≥1) |
| T5.4 | `industry=Roofing&location=Dallas Texas&limit=1000` | Returns ≤1000 results, no crash |

### Test Group 6 — API & Swagger

| # | Check | Pass Criteria |
|---|---|---|
| T6.1 | `GET /docs` | Loads without JavaScript errors |
| T6.2 | `GET /openapi.json` | Valid JSON, all endpoints documented |
| T6.3 | Request via Swagger UI for T1.1 | Returns valid JSON matching schema |
| T6.4 | `GET /health` | Returns 200 with service status |

### Test Group 7 — Logging & Structure

| # | Check | Pass Criteria |
|---|---|---|
| T7.1 | Run T1.1 and check server logs | Structured log lines at INFO level; no `print()` output |
| T7.2 | Check log format | JSON or consistent key=value format |
| T7.3 | Error scenarios logged | Failures appear as WARNING or ERROR, not silent |

---

## Execution Procedure

1. Start server: `python -m uvicorn app.main:app --reload`
2. Wait for "Uvicorn running on http://127.0.0.1:8000"
3. Verify health: `curl http://127.0.0.1:8000/health`
4. Run Test Groups 1–7 in order
5. Record results in the table below
6. If any test fails: fix, re-run full suite, record again

---

## Results Log

| Test Group | Status | Notes | Timestamp |
|---|---|---|---|
| Pre-Flight | ⬜ Pending | | |
| T1 — Trade Queries | ⬜ Pending | | |
| T2 — Limit Enforcement | ⬜ Pending | | |
| T3 — Deduplication | ⬜ Pending | | |
| T4 — Connector Metadata | ⬜ Pending | | |
| T5 — Edge Cases | ⬜ Pending | | |
| T6 — API & Swagger | ⬜ Pending | | |
| T7 — Logging | ⬜ Pending | | |

---

## Failure Resolution Protocol

If a test fails:

1. **Identify root cause** — Check server logs, not just the HTTP response
2. **Fix the code** — Make the minimal change needed
3. **Re-run the failing test** — Confirm it passes
4. **Re-run the full test group** — Ensure no regression
5. **Re-run the full suite** — Confirm no cross-group impact
6. **Record the fix** — Add note to this document

Never skip a failed test. Never mark a sprint complete with known failures.
