# Coding Rules

These rules apply to **all Python code** in the `backend/` directory. They were established during Sprint 1.1 and reinforced by the Sprint 1.2 Architecture Freeze.

---

## Language & Tooling

| Item             | Requirement                                      |
|------------------|--------------------------------------------------|
| Python version   | 3.12+                                            |
| Formatter        | Black (run before every commit)                  |
| Linter           | Ruff (zero-tolerance for violations)             |
| Type hints       | Mandatory on all functions, methods, variables   |
| Docstrings       | Google style for every public function/class     |
| Print statements | **Forbidden** in production code — use `logging` |

---

## File Organisation

Each module must have exactly one responsibility. A file should not mix concerns (e.g., don't put business logic inside an API route file).

```
app/
├── api/v1/        — FastAPI routers only (route → service call)
├── core/          — Settings, logging, constants, exceptions
├── database/      — SQLAlchemy engine + session
├── models/        — ORM classes only
├── schemas/       — Pydantic request/response schemas
├── repositories/  — Data access (SQL queries)
├── services/      — Business logic orchestration
├── engines/       — Domain-specific processing units
└── ai/            — AI providers, prompts, scoring, summarising
```

**Rule:** API routes call services; services call engines and repositories; engines and repositories do not call each other directly.

---

## Naming Conventions

| Element          | Convention              | Example                          |
|------------------|-------------------------|----------------------------------|
| Module names     | `snake_case`            | `company_discovery_engine.py`    |
| Class names      | `PascalCase`            | `CompanyDiscoveryEngine`         |
| Function names   | `snake_case`            | `discover_companies()`           |
| Constants        | `UPPER_SNAKE_CASE`      | `MAX_RETRIES = 3`                |
| Private attrs    | `_leading_underscore`   | `_config`                        |
| Test files       | `test_<module>.py`      | `test_company_discovery.py`      |

---

## Imports

- Use **absolute imports** from the project root (`app.*`).
- No wildcard imports (`from X import *`).
- Sort imports with Ruff's `I` rule (isort-compatible).
- Type-only imports that cause circular dependencies must use `from __future__ import annotations`.

---

## Error Handling

- Never swallow exceptions silently. Log and re-raise or return a structured error.
- Use custom exceptions from `app.core.exceptions` where appropriate.
- API endpoints must return proper HTTP status codes (`200`, `404`, `422`, `500`).

---

## Logging

Use Python's standard `logging` module — never `print()`.

```python
import logging
logger = logging.getLogger(__name__)

logger.info("Processing company %s", company_name)
logger.warning("Missing field: %s", field_name)
logger.error("Failed to connect to %s", url, exc_info=True)
```

Log level must match severity: `DEBUG` for trace data, `INFO` for normal operations, `WARNING` for recoverable issues, `ERROR` for failures.

---

## Testing

- Every new feature must have corresponding unit tests in `tests/`.
- Tests must cover: happy path, boundary conditions, error paths.
- Run `pytest` before marking any task complete.
- Tests must not depend on external network calls (mock them).

---

## Prohibited Practices

| Practice                  | Why                                        |
|---------------------------|--------------------------------------------|
| `print()` in production   | Pollutes stdout; use logging instead       |
| Circular imports          | Causes runtime import errors               |
| Hard-coded credentials    | Security risk; use env vars via config     |
| Raw SQL strings           | Bypasses ORM safety; use SQLAlchemy queries|
| Duplicate implementations | Violates DRY; merge into shared utilities  |
| TODO comments             | Must be resolved before sprint close       |
| Placeholder functions     | Must be replaced with real implementation  |

---

## Review Checklist

Before submitting any code change, verify:

- [ ] `ruff check .` passes with zero warnings
- [ ] `black .` has been run
- [ ] `pytest` passes
- [ ] No `print()` statements remain
- [ ] All public APIs have docstrings
- [ ] Type hints present on all new signatures
- [ ] No circular imports introduced
- [ ] No new dependencies without review
