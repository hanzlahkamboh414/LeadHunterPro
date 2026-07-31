# Sprint 2.1 — Company Discovery Engine

Status: READY

Priority: CRITICAL

Owner: Hanzlah

Architecture: CLAUDE.md

---

# Objective

Build the first production-ready Company Discovery Engine.

This engine is responsible for discovering construction companies from public sources based on the user's search request.

The engine MUST NOT use AI.

Its only job is discovering and validating companies.

---

# User Input

Example

Industry

Construction Estimating

Location

Dallas

Texas

USA

Limit

100

---

# Expected Output

[
  {
    "company_name": "",
    "website": "",
    "city": "",
    "state": "",
    "country": "",
    "source": "",
    "confidence": ""
  }
]

---

# Responsibilities

The engine must

Search public sources

Extract company names

Find official websites

Validate websites

Remove duplicates

Return clean structured data

---

# Supported Sources

Google Search

Bing Search

Business Directories

Official Company Websites

Government Contractor Lists

Public Construction Directories

The architecture must allow adding new sources later.

---

# Folder Structure

app/

engines/

discovery/

company/

company_discovery_engine.py

company_search.py

company_validator.py

company_cleaner.py

company_models.py

README.md

Only create these files.

No extra files.

---

# Engine Responsibilities

company_search.py

Search public sources.

Return raw companies.

No validation.

No AI.

---

company_validator.py

Verify

Website exists

Company name

Official domain

Remove fake entries

---

company_cleaner.py

Remove

Duplicate domains

Duplicate names

Invalid companies

Normalize company names.

---

company_discovery_engine.py

Main orchestrator.

Flow

Search

↓

Validate

↓

Clean

↓

Return

Never perform AI reasoning.

---

# Data Model

CompanyDiscoveryResult

Fields

company_name

website

city

state

country

source

confidence

No database writes.

---

# Validation Rules

Website must respond.

Company name cannot be empty.

Duplicate websites removed.

Duplicate company names removed.

Normalize

Inc.

LLC

Ltd.

Corporation

Limited

etc.

---

# Logging

Log

Search started

Source count

Companies found

Companies removed

Final count

Errors

Use structured logging.

Never use print().

---

# Error Handling

If one source fails

Continue.

Never stop discovery because one provider failed.

---

# Testing

Create unit tests.

Verify

Duplicate removal

Website validation

Invalid company removal

Empty search

No results

Large results

---

# Acceptance Criteria

Engine returns clean companies.

No duplicate companies.

No duplicate domains.

Website validation works.

All tests pass.

No architecture violations.

No circular imports.

No AI usage.

---

# Deliverables

company_discovery_engine.py

company_search.py

company_validator.py

company_cleaner.py

company_models.py

Unit tests

Sprint report

Test report

Architecture report

Performance report

---

# Definition of Done

Claude must

Implement

Run tests

Fix errors

Run tests again

Repeat until

All tests pass.

Only then mark sprint complete.

Never leave known bugs.

Never leave TODO comments.

Production-quality code only.



Read CLAUDE.md first.

Then read:

docs/sprints/Sprint-02.1-Company-Discovery-Engine.md

Follow both documents STRICTLY.

DO NOT invent architecture.

DO NOT create extra folders.

DO NOT create extra files.

DO NOT modify the project structure.

Implement ONLY Sprint 2.1.

======================================================
DEVELOPMENT WORKFLOW (MANDATORY)
======================================================

1. Read CLAUDE.md completely.

2. Read Sprint-02.1 completely.

3. Review the current project architecture.

4. Implement ONLY the required files.

5. Keep code production quality.

6. Use dependency injection.

7. Use structured logging.

8. No print() statements.

9. No TODO comments.

10. No placeholder implementations.

======================================================
MANDATORY TESTING
======================================================

After implementation DO NOT stop.

Run all verification steps.

1.

ruff check .

2.

black .

3.

pytest

4.

Start FastAPI

python -m uvicorn app.main:app --reload

5.

Verify

• imports

• routing

• dependency injection

• startup

• database connection

6.

If ANY error exists

DO NOT STOP.

Fix the error.

Run all tests again.

Repeat until everything passes.

======================================================
QUALITY RULES
======================================================

No duplicate code.

No circular imports.

No unused imports.

No dead code.

No architecture violations.

Follow SOLID.

Follow Clean Architecture.

Follow CLAUDE.md.

======================================================
REQUIRED REPORTS
======================================================

At the end generate:

1.

Sprint Report

Include

• Files Created

• Files Modified

• Summary

2.

Test Report

Include

• Ruff

• Black

• Pytest

• FastAPI Startup

• API Verification

3.

Architecture Report

Verify

• Folder responsibilities

• Dependency direction

• No circular imports

4.

Performance Report

Include

• Companies discovered

• Duplicates removed

• Execution time

======================================================
FINAL RULE
======================================================

Do NOT mark the sprint complete until ALL tests pass.

If one test fails,
the sprint is NOT complete.

Status must be:

VERIFIED ✅

NOT "Done".