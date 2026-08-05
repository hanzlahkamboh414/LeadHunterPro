# Sprint 2.2A.1 — Engineering Principles Report

**Date:** 2026-08-03  
**Type:** Documentation Only (No Code)  
**Status:** COMPLETE

---

## Sections Created

The following sections were written into `docs/architecture/PROJECT_PRINCIPLES.md`:

| Section | Content |
|---|---|
| **1. Purpose** | Explains why these principles exist, their permanence, and the failures that shaped them |
| **2. Engineering Principles** | 10 principles: Single Responsibility, Separation of Concerns, Dependency Direction, Replaceable Components, Production Data Policy, Error Handling, Scalability, Testability, Documentation First, No Rewrite Rule |
| **3. Code Quality Standards** | Language/tooling table + prohibited patterns with alternatives |
| **4. Dependency Map** | Allowed imports diagram + forbidden import examples |
| **5. Connector Contract** | BaseConnector interface specification + implementation rules |
| **6. Testing Standards** | Test organization, categories, coverage targets, assertion requirements |
| **7. API Design Standards** | Response format schema, error response matrix, request validation rules |
| **8. Naming Conventions** | Module, class, function, constant, test file, ADR, and archive naming rules |
| **9. Logging Standards** | Logger setup pattern, log level usage table, forbidden patterns |
| **10. Future Engineering Rules** | Required sprint sequence with step-by-step workflow |
| **11. Definition of Engineering Done** | 9-item checklist that must all pass before any feature is shipped |
| **12. Amendments** | Process for changing principles (review → ADR → founder approval) |

---

## Engineering Rules Documented

| # | Principle | Status |
|---|---|---|
| 1 | Single Responsibility Principle | Documented |
| 2 | Separation of Concerns (API → Engine → Connector → Crawler → Utilities) | Documented |
| 3 | Dependency Direction (downward only, no circular imports) | Documented |
| 4 | Replaceable Components (connectors and AI providers interchangeable) | Documented |
| 5 | Production Data Policy (fixtures only in tests/CI, never production) | Documented |
| 6 | Error Handling (log everything, retry external calls, fail open) | Documented |
| 7 | Scalability (100→1,000 connectors, millions of companies, no redesign) | Documented |
| 8 | Testability (isolated tests, mockable deps, coverage targets) | Documented |
| 9 | Documentation First (no feature without doc update) | Documented |
| 10 | No Rewrite Rule (fix architecture, don't patch around it) | Documented |

---

## Files Modified

**None.** Zero backend files were touched. Zero API changes. Zero database changes. Zero test modifications.

---

## Files Created

| File | Lines | Purpose |
|---|---|---|
| `docs/architecture/PROJECT_PRINCIPLES.md` | ~340 | Permanent engineering constitution -- highest-level document in the project |

---

## Files Referenced (Read-Only)

| File | Why Read |
|---|---|
| `docs/architecture/discovery_architecture.md` | Ensured principles align with documented architecture |
| `docs/decisions/ADR-001-Connector-Architecture.md` | Cross-referenced connector contract |
| `docs/decisions/ADR-002-No-Production-Fixtures.md` | Validated production data policy consistency |
| `CLAUDE.md` | Verified alignment with existing mandatory rules |

---

## Conflicts Checked

| Potential Conflict | Resolution |
|---|---|
| Principles vs. existing CLAUDE.md rules | Aligned -- CLAUDE.md rules are a subset; principles expand and formalize them |
| Principles vs. current dual-SDK state | Principles explicitly forbid the dual-SDK anti-pattern (violation of Replaceable Components + Single Responsibility) |
| Principles vs. current fixture dependency | Principles explicitly forbid production fixtures (ADR-002 already established this) |
| Principles vs. existing test structure | Principles reinforce existing test requirements; no conflicts |

No conflicts found. Principles are consistent with all existing documentation and codebase rules.

---

## Next Sprint

Sprint 2.2B (implementation) will begin when approved. It will implement:
1. Crawler layer (`app/crawlers/`)
2. Industry expansion module (`app/connectors/industry_expansion.py`)
3. Expanded Texas Procurement connector (500+ curated fixtures)
4. Unified connector framework (single BaseConnector, deleted adapters)
5. Async validator with domain cache

All implementation will follow the principles defined in this document.
