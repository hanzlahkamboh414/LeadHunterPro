# Connector SDK

## Purpose

The Connector SDK provides a unified interface for discovering construction companies from diverse public sources (procurement portals, trade directories, government bid databases). It replaces ad-hoc crawling with a pluggable, testable, and maintainable architecture.

---

## Core Concepts

### BaseConnector (`app/engines/source_connectors/base.py`)

Abstract base class that every connector must implement. Defines the contract:

```python
class ConstructionSourceConnector(ABC):
    @property
    def name(self) -> str: ...       # e.g. "texas_procurement"

    @property
    def description(self) -> str: ... # one-line summary

    def is_available(self) -> bool: ...  # environment check

    @abstractmethod
    def discover(self, *, state, city, industry, limit) -> tuple[list[dict], dict]:
        ...
```

### CompanyResult (`app/engines/source_connectors/sdk.py`)

Frozen dataclass representing a single company record returned by any connector:

```python
@dataclass(frozen=True)
class CompanyResult:
    company_name: str
    website: str
    city: str
    state: str
    country: str = "USA"
    source_url: str = ""
    industry_focus: str = ""
    revenue_tier: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
```

### ConnectorRegistry (`app/engines/source_connectors/sdk.py`)

Singleton registry that tracks all active connectors:

```python
ConnectorRegistry.register(connector)
ConnectorRegistry.get("texas_procurement")
ConnectorRegistry.list_all()
ConnectorRegistry.list_names()
```

---

## SDK Utilities (`sdk_utils.py`)

| Component            | Purpose                                          |
|----------------------|--------------------------------------------------|
| `ConnectorLogger`    | Structured logger scoped to each connector       |
| `connector_retry()`  | Decorator adding exponential-backoff retries     |
| `ConnectorErrorHandler` | Centralised error/warning collection           |
| `HTTPClient`         | Session-managed HTTP client with rate limiting   |

---

## Registered Connectors

### Texas Procurement (`texas_procurement.py`)

Discovers construction companies from a curated dataset of verified Texas contractor records. Acts as a replacement for web-search-based discovery in geographies where procurement portals are the primary source.

**Filtering:** state, city, industry keyword matching.

**Data source:** Curated fixture data (see `_TAXAS_CONSTRUCTION_COMPANIES`). In production this would be populated by scraping TxDOT, state contracts, and county bid databases.

---

## Extending the SDK

To add a new connector:

1. Create a new module in `app/engines/source_connectors/`.
2. Import and subclass `ConstructionSourceConnector`.
3. Implement `name`, `description`, `is_available()`, and `discover()`.
4. Return `list[CompanyResult]` (or raw dicts for the legacy interface).
5. Register in `sdk.py`: `ConnectorRegistry.register(MyConnector())`.

---

## API Endpoint

```
GET /api/v1/connectors/texas-procurement?state=TX&city=Dallas&industry=Construction Estimating&limit=50
GET /api/v1/connectors/texas-procurement/summary
```

Full router: `app/api/v1/connectors.py`

---

## Current Limitations

- Texas Procurement connector uses fixture data (not live scraping).
- No authentication-gated sources supported yet.
- Rate limiting is per-connector, not global.
