# Sprint 1.2 – Architecture Freeze

Status: Required

---

# Objective

This sprint does NOT add new features.

This sprint exists to clean and freeze the project architecture before future development.

No business logic should be added.

---

# Tasks

## 1. Remove Duplicate Architecture

Find duplicate implementations and merge them.

Examples:

- website/
- research/website/

Only ONE implementation may exist.

The same rule applies to:

- parser
- crawler
- reports
- ai
- analyzer

---

## 2. Folder Responsibilities

Every folder must have exactly one responsibility.

Example

api/
Only HTTP.

services/
Business logic.

repositories/
Database only.

engines/
Business engines only.

crawler/
Raw HTML download only.

---

## 3. Remove Future Features

If a file belongs to a future sprint,
do not delete it immediately.

Instead:

- Move it into a placeholder state
OR
- Remove implementation but keep interfaces.

Future sprint code must not contain active business logic.

---

## 4. Database Review

Verify models against architecture.

Current approved models are:

- Company
- Leadership
- Research
- Evidence
- LeadIntelligence

If other models exist:

Do NOT remove automatically.

Generate a report explaining:

- Why they exist.
- Whether they should stay.
- Which sprint they belong to.

---

## 5. AI Review

Verify that:

AI never performs:

- Crawling
- Scraping
- Database access

AI must only:

- Reason
- Score
- Summarize

---

## 6. Engine Review

Every engine must have exactly one responsibility.

Discovery

Research

Compiler

Intelligence

Ranking

No engine may perform another engine's work.

---

## 7. Circular Import Audit

Verify there are no circular imports.

Generate report.

---

## 8. Dependency Audit

API

↓

Services

↓

Repositories

↓

Database

Verify dependency direction.

---

## 9. Produce Architecture Report

At the end create

architecture_report.md

Include

Current folders

Duplicate folders

Violations

Recommendations

Approved architecture

Nothing should be modified automatically without explanation.

---

## Acceptance Criteria

The sprint succeeds only if

✔ Duplicate architecture identified.

✔ Folder responsibilities verified.

✔ Dependency direction verified.

✔ Circular imports checked.

✔ AI responsibility verified.

✔ Architecture report generated.

No new features should be implemented.