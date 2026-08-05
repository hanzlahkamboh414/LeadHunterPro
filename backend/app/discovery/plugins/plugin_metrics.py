"""Per-plugin execution metrics.

Metrics answer a single question: *is this plugin actually pulling its
weight?* They are recorded per plugin, per run, and are keyed off the
Phase 1 :class:`SourceStatus` contract so that every plugin — present
or future — is measured the same way.

This is deliberately separate from
``app.engines.discovery.company.company_models.DiscoveryMetrics``,
which accumulates totals for one whole discovery *run* across the
pipeline. :class:`PluginMetrics` is scoped to one *plugin* across many
runs. The two do not overlap and neither can be expressed in terms of
the other.

Metrics are observational only. Nothing in the framework changes
routing based on them — that would hide failures behind automatic
behaviour, which CLAUDE.md §12 forbids.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.discovery.sources.status import SourceStatus

logger = logging.getLogger(__name__)


@dataclass
class PluginMetrics:
    """Cumulative execution counters for one plugin.

    Attributes:
        plugin_name: Owning plugin identifier.
        total_runs: Number of completed ``discover()`` calls.
        success_count: Runs that returned :attr:`SourceStatus.SUCCESS`.
        error_count: Runs that returned :attr:`SourceStatus.ERROR`.
        empty_runs: Runs that returned :attr:`SourceStatus.EMPTY`.
        unavailable_count: Runs that returned
            :attr:`SourceStatus.UNAVAILABLE`.
        companies_found: Companies returned across all runs.
        duplicates_removed: Duplicates the plugin filtered *within its
            own result set*. Reported into the manager via the
            ``metadata["duplicates_removed"]`` field; cross-plugin
            deduplication remains the orchestrator's job.
        average_latency: Running mean wall-clock duration in
            milliseconds. Recomputed on every :meth:`record`.
        last_run: Unix timestamp of the most recent run.
        last_success: Unix timestamp of the most recent SUCCESS.
        last_error: Unix timestamp of the most recent ERROR.
        last_status: Status of the most recent run.
        total_latency_ms: Summed wall-clock time. Exposed for computing
            the mean; most consumers want :attr:`average_latency`.
    """

    plugin_name: str
    total_runs: int = 0
    success_count: int = 0
    error_count: int = 0
    empty_runs: int = 0
    unavailable_count: int = 0
    companies_found: int = 0
    duplicates_removed: int = 0
    average_latency: float = 0.0
    last_run: float | None = None
    last_success: float | None = None
    last_error: float | None = None
    last_status: SourceStatus | None = None
    total_latency_ms: float = 0.0

    #: Maps a status onto the counter field it increments.
    _STATUS_FIELDS = {
        SourceStatus.SUCCESS: "success_count",
        SourceStatus.EMPTY: "empty_runs",
        SourceStatus.UNAVAILABLE: "unavailable_count",
        SourceStatus.ERROR: "error_count",
    }

    def record(
        self,
        *,
        status: SourceStatus,
        companies: int = 0,
        duplicates: int = 0,
        latency_ms: float = 0.0,
        timestamp: float | None = None,
    ) -> None:
        """Record the outcome of one ``discover()`` call.

        Args:
            status: Status returned by the plugin.
            companies: Number of companies the plugin returned.
            duplicates: Duplicates the plugin removed from its own
                result set before returning it.
            latency_ms: Wall-clock duration of the call.
            timestamp: Unix time of the run. Defaults to now.
        """
        ts = time.time() if timestamp is None else timestamp
        self.total_runs += 1
        self.companies_found += max(0, companies)
        self.duplicates_removed += max(0, duplicates)
        self.total_latency_ms += max(0.0, latency_ms)
        self.average_latency = (
            self.total_latency_ms / self.total_runs if self.total_runs else 0.0
        )
        self.last_status = status
        self.last_run = ts

        if status == SourceStatus.SUCCESS:
            self.last_success = ts
        elif status == SourceStatus.ERROR:
            self.last_error = ts

        counter = self._STATUS_FIELDS.get(status)
        if counter is None:
            # Unknown status: still counted as a run, but flagged loudly
            # rather than silently discarded.
            logger.warning(
                "Plugin %s reported unrecognized status %r; "
                "counted as a run but not attributed to any status bucket",
                self.plugin_name,
                status,
            )
            return
        setattr(self, counter, getattr(self, counter) + 1)

    @property
    def failure_count(self) -> int:
        """Runs that neither succeeded nor legitimately returned nothing."""
        return self.unavailable_count + self.error_count

    @property
    def success_rate(self) -> float:
        """Fraction of runs that returned companies (0.0 - 1.0)."""
        if self.total_runs == 0:
            return 0.0
        return self.success_count / self.total_runs

    @property
    def avg_companies_per_run(self) -> float:
        """Mean number of companies returned per run."""
        if self.total_runs == 0:
            return 0.0
        return self.companies_found / self.total_runs

    @property
    def deduplication_rate(self) -> float:
        """Fraction of found companies that were duplicates (0.0 - 1.0)."""
        total_before_dedup = self.companies_found + self.duplicates_removed
        if total_before_dedup == 0:
            return 0.0
        return self.duplicates_removed / total_before_dedup

    def reset(self) -> None:
        """Zero every counter, keeping the plugin name."""
        self.total_runs = 0
        self.success_count = 0
        self.error_count = 0
        self.empty_runs = 0
        self.unavailable_count = 0
        self.companies_found = 0
        self.duplicates_removed = 0
        self.average_latency = 0.0
        self.total_latency_ms = 0.0
        self.last_status = None
        self.last_run = None
        self.last_success = None
        self.last_error = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize metrics for diagnostics and API output."""
        return {
            "plugin_name": self.plugin_name,
            "total_runs": self.total_runs,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "empty_runs": self.empty_runs,
            "unavailable_count": self.unavailable_count,
            "failure_count": self.failure_count,
            "companies_found": self.companies_found,
            "duplicates_removed": self.duplicates_removed,
            "deduplication_rate": round(self.deduplication_rate, 3),
            "average_latency": round(self.average_latency, 1),
            "avg_companies_per_run": round(self.avg_companies_per_run, 2),
            "success_rate": round(self.success_rate, 3),
            "last_status": (
                self.last_status.value if self.last_status else None
            ),
            "last_run": self.last_run,
            "last_success": self.last_success,
            "last_error": self.last_error,
        }

    def __repr__(self) -> str:
        last = self.last_status.value if self.last_status else "never"
        return (
            f"<PluginMetrics plugin={self.plugin_name!r}"
            f" runs={self.total_runs}"
            f" companies={self.companies_found}"
            f" success_rate={self.success_rate:.2f}"
            f" last={last}>"
        )
