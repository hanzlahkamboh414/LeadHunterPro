# Connectors Module

Unified connector architecture for external data sources.

## Structure

```
app/connectors/
├── __init__.py              # Package exports
├── base_connector.py        # BaseConnector ABC
├── connector_registry.py    # Registry for connectors
├── connector_manager.py     # Discovery orchestration
└── connector_result.py      # Result dataclass
```

## Usage

```python
from app.connectors import ConnectorRegistry, ConnectorManager, BaseConnector
from app.connectors.connector_result import ConnectorResult

# Register a connector
class MyConnector(BaseConnector):
    connector_name = "my_source"
    priority = 10
    enabled = True

    def search(self, industry, location, limit):
        ...

    def health_check(self):
        ...

    def validate_result(self, result):
        ...

ConnectorRegistry.register(MyConnector())

# Execute discovery
manager = ConnectorManager()
results, metadata = manager.discover(
    industry="Construction Estimating",
    location="Dallas Texas USA",
    limit=100,
)
```

## Rules

- Connectors NEVER call each other directly.
- ConnectorManager NEVER knows connector-specific logic.
- All connectors must implement the BaseConnector interface.
- Results are validated before being accepted.
- Duplicates are removed by domain and normalized name.
