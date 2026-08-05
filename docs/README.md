# Documentation Index

This directory contains all project documentation for LeadHunter Pro.

---

## Directory Structure

```
docs/
├── architecture/        # System design, data models, interface contracts
├── decisions/           # Architectural Decision Records (ADRs)
├── qa/                  # Manual QA plans, bug reports, test evidence
├── archive/             # Superseded sprint documents and obsolete designs
├── templates/           #Reusable document templates
└── sprints/             # Sprint planning and tracking documents
    └── archive/         # Archived sprint documents
```

---

## What Belongs Where

### `architecture/`

Permanent design references that define HOW the system is built.

Include:
- System layout and component diagrams
- Database schema and model definitions
- Interface contracts and API specifications
- Connector SDK architecture
- Coding standards and conventions
- Testing strategy
- Any blueprint document that guides implementation

Do NOT include:
- Sprint-level tasks (go in `sprints/`)
- Decision rationales (go in `decisions/`)
- Bug reports (go in `qa/`)
- Obsolete designs (go in `archive/`)

### `decisions/`

Architectural Decision Records (ADRs) that capture WHY a particular approach was chosen.

Include:
- Every significant architectural choice
- Options considered and rejected
- Trade-off analysis
- Consequences (positive and negative)
- Future impact assessment

Naming convention: `ADR-NNN-ShortDescriptiveTitle.md`

Do NOT include:
- Trivial decisions (naming choices, variable names)
- Implementation details (go in `architecture/`)
- Sprint tasks (go in `sprints/`)

### `qa/`

Quality assurance evidence and testing artifacts.

Include:
- Manual QA plans with expected vs actual results
- Bug reports with reproduction steps
- Test coverage summaries
- Performance benchmarks
- Pre-sprint verification checklists

Naming convention: `YYYY-MM-DD-short-description.md`

Do NOT include:
- Unit test code (go in `backend/tests/`)
- Architecture decisions (go in `decisions/`)
- Sprint plans (go in `sprints/`)

### `archive/`

Documents that have been superseded but should be preserved for historical context.

Include:
- Old sprint documents (original Sprint 2.2 → archive)
- Replaced architecture diagrams
- Obsolete design proposals
- Deprecated API specifications

Naming convention: Keep original filename; add `-archived-YYYY-MM-DD` suffix if ambiguous.

Do NOT include:
- Current sprint documents (go in `sprints/`)
- Active architecture references (go in `architecture/`)

### `templates/`

Reusable document templates for consistent formatting.

Include:
- ADR template (`ADR_TEMPLATE.md`)
- Sprint template (future)
- Bug report template (future)
- Architecture review template (future)

Do NOT include:
- Completed documents (fill the template, then move to the appropriate directory)

### `sprints/`

Active sprint planning documents.

Include:
- Current sprint scope and tasks
- Sprint objectives and acceptance criteria
- Source validation reports
- Migration plans

Naming convention: `sprint_<number>_<title>.md` or `Sprint-<X.Y>-<Title>.md` (matching existing convention)

Moved to `sprints/archive/` when a sprint is cancelled or completed and replaced.

---

## Cross-References

| Document | References | Referenced By |
|---|---|---|
| [architecture.md](architecture/architecture.md) | This index | CLAUDE.md |
| [database.md](architecture/database.md) | This index | architecture.md |
| [connector_sdk.md](architecture/connector_sdk.md) | ADR-001 | architecture.md, sprint_2_2_real_discovery.md |
| [coding_rules.md](architecture/coding_rules.md) | This index | CLAUDE.md |
| [testing.md](architecture/testing.md) | This index | CLAUDE.md |
| [discovery_architecture.md](architecture/discovery_architecture.md) | ADR-001, ADR-002 | sprint_2_2_real_discovery.md |
| [review_discovery_redesign.md](architecture/review_discovery_redesign.md) | — | sprint_2_2_real_discovery.md |
| [ADR-001-Connector-Architecture.md](../decisions/ADR-001-Connector-Architecture.md) | discovery_architecture.md | — |
| [ADR-002-No-Production-Fixtures.md](../decisions/ADR-002-No-Production-Fixtures.md) | discovery_architecture.md | — |
| [source_validation_report.md](../sprints/source_validation_report.md) | ADR-001, ADR-002 | sprint_2_2_real_discovery.md |
| [sprint_2_2_real_discovery.md](sprints/sprint_2_2_real_discovery.md) | All of the above | CLAUDE.md (future update) |
| [sprint_2_2_sdk.md (archived)](sprints/archive/sprint_2_2_sdk.md) | — | — |
