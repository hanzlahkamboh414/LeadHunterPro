"""Discovery source status types.

Every source must return one of these statuses so the orchestrator can
make deterministic fallback decisions without any source being a
required dependency.
"""

from __future__ import annotations

from enum import Enum, auto


class SourceStatus(str, Enum):
    """Return status from a source's discover() call."""

    #: Source executed successfully and returned companies.
    SUCCESS = "success"
    #: Source executed but found no matching companies.
    EMPTY = "empty"
    #: Source is configured but currently unreachable
    #: (DNS failure, timeout, connection refused).
    UNAVAILABLE = "unavailable"
    #: Source threw an unexpected exception.
    ERROR = "error"


class SourceHealth(str, Enum):
    """Health check result for the source health report."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"
