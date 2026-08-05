# Testing Strategy

LeadHunter Pro uses **pytest** as its testing framework. Tests live alongside the code they cover, under `backend/tests/`.

---

## Test Structure

```
backend/tests/
└── discovery/
    ├── test_company_discovery.py    # Company discovery engine
    ├── test_source_connectors.py    # Source connector unit tests
    ├── test_source_intelligence.py  # Source intelligence planner
    └── test_sdk_base.py             # SDK base-class contract tests
```

---

## Running Tests

```bash
# Run all tests
pytest

# Run a specific file
pytest tests/discovery/test_company_discovery.py -v

# Run with coverage
pytest --cov=app --cov-report=term-missing
```

---

## Test Categories

### Unit Tests
Test individual functions and classes in isolation. Mock all external dependencies (HTTP calls, database, AI providers).

```python
@pytest.fixture
def mock_http_client(mocker):
    mocker.patch("app.engines.source_connectors.sdk_utils.HTTPClient.get")
```

### Integration Tests
Verify that modules work together correctly (e.g., engine → cleaner → metrics). These may make real HTTP calls to local/mock services.

### Contract Tests
Ensure connectors adhere to the `BaseConnector` / `ConstructionSourceConnector` interface (the `test_sdk_base.py` suite).

---

## Assertions to Cover

For every new feature, tests must verify:

| Category          | What to test                                      |
|-------------------|---------------------------------------------------|
| Happy path        | Expected output given valid input                 |
| Boundary values   | Empty strings, max limits, zero results           |
| Error handling    | Graceful degradation when dependencies fail       |
| Deduplication     | Duplicate entities are collapsed correctly        |
| Validation        | Invalid inputs produce expected errors            |
| Metrics tracking  | Counts, rates, and summary data are accurate      |

---

## Quality Gates

Tests are part of the definition of done. A sprint is not complete until:

1. `ruff check .` passes with **zero** issues.
2. `black .` formatting is applied.
3. `pytest` passes with **all** tests green.
4. No `print()` statements exist in production or test code.

If any test fails, fix it and re-run the full suite — never leave known failures.

---

## Mocking External Dependencies

Always mock these to keep tests fast and deterministic:

| Dependency          | Mock strategy                              |
|---------------------|--------------------------------------------|
| HTTP requests       | `unittest.mock.patch` on `requests.Session`|
| Database sessions   | In-memory SQLite or pytest fixtures        |
| AI providers        | Stub `generate()` returning fixed text     |
| Filesystem         | `tmp_path` fixture for temp files          |

---

## Existing Test Coverage

| Module                       | Tests | Status |
|------------------------------|-------|--------|
| Company Discovery Engine     | 17    | ✅ All pass |
| Source Connectors (SDK)      | N/A   | ✅ Contracts verified |
| Source Intelligence          | N/A   | ✅ Integrated |

See individual test files for full coverage details.
