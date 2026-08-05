# LeadHunterPro — Project Development & Implementation Principles

## Purpose

This document defines the development philosophy for the LeadHunterPro project.

These rules override any AI assumptions.

If the specification conflicts with an assumption, **the specification always wins**.

---

# 1. Project Philosophy

LeadHunterPro is built incrementally.

Each Unit delivers **one clearly defined responsibility**.

The objective is not to finish the whole feature in one Unit.

The objective is to build a stable architecture one layer at a time.

Future Units are expected to build on previous Units.

Do not implement future work inside the current Unit.

---

# 2. Single Responsibility Principle (SRP)

Every Unit must own **exactly one responsibility**.

Never combine multiple responsibilities into one implementation unless the specification explicitly requires it.

Examples:

* Plugin
* Generator
* Extractor
* Configuration
* Orchestrator
* Validation
* Storage

Each of these belongs to its own module.

Large "God Classes" or "God Files" are not allowed.

---

# 3. Scope Rule

Implement **ONLY** what the specification explicitly requires.

Never implement:

* future features
* optional improvements
* anticipated requirements
* "helpful" additions
* speculative architecture

If something is not explicitly required, do not implement it.

---

# 4. Frozen Units Rule

Completed Units are considered frozen.

Never modify a completed Unit unless explicitly instructed.

If implementing the current Unit appears to require changing a previous Unit:

STOP.

Explain why.

Wait for approval.

---

# 5. Architecture Rule

Prefer composition over large files.

Preferred structure:

Plugin
↓

Generator
↓

Extractor
↓

Models

Do not merge unrelated responsibilities into one file.

Keep coupling low.

Keep cohesion high.

---

# 6. Module Ownership

Each module owns one responsibility only.

Examples:

Unit 6
Owns Candidate interfaces.

Unit 7
Owns configuration views.

Unit 8
Owns plugin orchestration.

Future Units
Own generators.

Future Units
Own extractor implementations.

Never move another Unit's responsibility into the current Unit.

---

# 7. File Size Guideline

Preferred:

100–300 LOC

Acceptable:

Below 500 LOC

If a file is expected to exceed 500 lines:

Explain why.

If possible split into multiple modules.

Never create extremely large files simply for convenience.

---

# 8. Review Before Implementation

Before writing any code:

Separate:

FACTS

from

ASSUMPTIONS.

Anything not explicitly stated in the specification must be marked:

ASSUMPTION

Assumptions must never become implementation.

---

# 9. Implementation Order

Always build in layers.

Interfaces first.

Models second.

Infrastructure third.

Implementations fourth.

Integration last.

Never reverse this order.

---

# 10. AI Behaviour Rules

When uncertain:

STOP.

Do not guess.

Do not invent.

Do not assume.

Ask for clarification.

It is better to stop than to implement incorrect architecture.

---

# 11. Specification Compliance

The implementation must follow the specification exactly.

Do not replace specification requirements with personal preferences.

Recommendations are welcome.

Implementation is based only on approved requirements.

---

# 12. Backward Compatibility

Public APIs from completed Units must remain stable.

Never remove.

Never rename.

Never silently change behaviour.

Backward compatibility is mandatory unless explicitly approved.

---

# 13. Dependency Rules

Respect existing architecture.

Do not introduce circular dependencies.

Do not bypass abstraction layers.

Do not import future Units.

Only depend on verified completed Units.

---

# 14. Implementation Strategy

Every Unit should be independently testable.

Every Unit should be independently reviewable.

Every Unit should be independently verifiable.

A Unit should be considered complete only after:

* Unit tests pass
* Regression tests pass
* Lint passes
* Public API verification passes
* Architecture review passes
* Specification compliance review passes

---

# 15. Development Workflow

The required workflow is:

1. Read Specification
2. Read Project Rules
3. Verify Dependencies
4. Separate Facts from Assumptions
5. Produce Pre-Implementation Review
6. Wait for Approval
7. Implement ONLY Approved Scope
8. Stop
9. Wait for Verification
10. Only after approval proceed to the next Unit

Never skip steps.

Never continue automatically to the next Unit.

---

# 16. Success Criteria

Good code is NOT measured by how much code is written.

Good code is measured by:

* Correct scope
* Clean architecture
* Single responsibility
* Small maintainable modules
* Stable public APIs
* Zero unnecessary features
* Full specification compliance
* Minimal technical debt

Always optimize for long-term maintainability rather than short-term completeness.
