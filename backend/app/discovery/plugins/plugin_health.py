"""Per-plugin health tracking.

Health answers a different question than metrics: *should we still
trust this plugin right now?* It is derived from the outcome of recent
runs and from explicit ``health_check()`` calls.

Two vocabularies, deliberately
------------------------------
:class:`PluginState` is the framework's own five-state model. Phase 1's
:class:`~app.discovery.sources.status.SourceHealth` only has
HEALTHY / UNHEALTHY / UNKNOWN, which cannot express "working but
impaired" or "deliberately out of service".

Rather than modify the Phase 1 enum — which is forbidden, and which
would change behaviour for every existing source — :class:`PluginHealth`
stores the richer :class:`PluginState` and exposes ``status`` as a
*derived*, read-only :class:`SourceHealth`. Existing consumers reading
``health.status`` keep working unchanged and see exactly the verdict
they saw before.

The mapping is chosen to preserve prior behaviour exactly:

    ===================  ==================  ==============================
    PluginState          SourceHealth        Rationale
    ===================  ==================  ==============================
    HEALTHY              HEALTHY             unchanged
    DEGRADED             UNHEALTHY           any failure already read as
                                             UNHEALTHY, so mapping DEGRADED
                                             here is a strict information
                                             *gain* with zero behaviour
                                             change
    UNAVAILABLE          UNHEALTHY           unchanged
    DISABLED             UNKNOWN             not running is not broken
    MAINTENANCE          UNKNOWN             deliberately out of service
    UNKNOWN              UNKNOWN             unchanged
    ===================  ==================  ==============================

Mapping DEGRADED onto HEALTHY was rejected: a single transient error
would then flip a legacy consumer's verdict from unhealthy to healthy,
which is precisely the kind of silent behaviour change CLAUDE.md §12
forbids.

Health is recorded, never acted on. Nothing here auto-disables a
plugin — that would hide a failure behind automatic behaviour. Health
is a diagnostic signal for humans and for the orchestrating layer to
act on explicitly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.discovery.sources.status import SourceHealth, SourceStatus

logger = logging.getLogger(__name__)


class PluginState(str, Enum):
    """Fine-grained health state of a plugin."""

    #: Last run succeeded (or correctly found nothing).
    HEALTHY = "healthy"
    #: Failing intermittently but still worth calling.
    DEGRADED = "degraded"
    #: Failing consistently; treat as down.
    UNAVAILABLE = "unavailable"
    #: Switched off by configuration or an operator.
    DISABLED = "disabled"
    #: Deliberately taken out of service (upstream migration, etc.).
    MAINTENANCE = "maintenance"
    #: Never run and never probed — no information either way.
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        """Render as the bare value, not ``PluginState.X``."""
        return self.value


#: Translation to the Phase 1 vocabulary. See the module docstring for
#: why each row is what it is.
STATE_TO_SOURCE_HEALTH: dict[PluginState, SourceHealth] = {
    PluginState.HEALTHY: SourceHealth.HEALTHY,
    PluginState.DEGRADED: SourceHealth.UNHEALTHY,
    PluginState.UNAVAILABLE: SourceHealth.UNHEALTHY,
    PluginState.DISABLED: SourceHealth.UNKNOWN,
    PluginState.MAINTENANCE: SourceHealth.UNKNOWN,
    PluginState.UNKNOWN: SourceHealth.UNKNOWN,
}

#: Statuses that count as the plugin being functional. EMPTY is healthy:
#: "I ran and there was genuinely nothing to find" is a correct answer.
HEALTHY_STATUSES = frozenset({SourceStatus.SUCCESS, SourceStatus.EMPTY})

#: States in which an operator has deliberately parked the plugin. A
#: recorded run clears these, because a plugin that ran is evidently
#: neither disabled nor in maintenance.
PARKED_STATES = frozenset({PluginState.DISABLED, PluginState.MAINTENANCE})

#: Consecutive failures before a plugin is considered UNAVAILABLE rather
#: than merely DEGRADED.
DEFAULT_UNAVAILABLE_THRESHOLD: int = 3


def normalize_state(value: PluginState | str) -> PluginState:
    """Coerce a plugin-reported state string into a :class:`PluginState`.

    Args:
        value: Enum member, or its string value (case-insensitive).

    Returns:
        The matching :class:`PluginState`, or
        :attr:`PluginState.UNKNOWN` when the string is unrecognized.
    """
    if isinstance(value, PluginState):
        return value
    if isinstance(value, str):
        try:
            return PluginState(value.strip().lower())
        except ValueError:
            logger.warning(
                "Unrecognized plugin state %r; treating as UNKNOWN", value
            )
            return PluginState.UNKNOWN
    raise TypeError(
        f"State must be a PluginState or str, got {type(value).__name__}"
    )


@dataclass
class PluginHealth:
    """Current health state of one plugin.

    Attributes:
        plugin_name: Owning plugin identifier.
        state: Fine-grained health state.
        consecutive_failures: Failures since the last healthy result.
        last_checked_at: Unix timestamp of the last health update.
        message: Human-readable diagnostic for the current state.
        details: Optional structured diagnostics from ``health_check()``.
        unavailable_threshold: Consecutive failures at which the state
            escalates from DEGRADED to UNAVAILABLE.
    """

    plugin_name: str
    state: PluginState = PluginState.UNKNOWN
    consecutive_failures: int = 0
    last_checked_at: float | None = None
    message: str = ""
    details: dict[str, Any] | None = None
    unavailable_threshold: int = DEFAULT_UNAVAILABLE_THRESHOLD

    # ------------------------------------------------------------------
    # Derived views
    # ------------------------------------------------------------------

    @property
    def status(self) -> SourceHealth:
        """Phase 1 view of this health state.

        Read-only and derived, so the two vocabularies can never drift.
        """
        return STATE_TO_SOURCE_HEALTH.get(self.state, SourceHealth.UNKNOWN)

    @property
    def is_healthy(self) -> bool:
        """True only when health has been positively confirmed."""
        return self.state == PluginState.HEALTHY

    @property
    def is_degraded(self) -> bool:
        """True when the plugin is failing but still worth calling."""
        return self.state == PluginState.DEGRADED

    @property
    def is_operational(self) -> bool:
        """True when the plugin is worth calling at all.

        HEALTHY and DEGRADED are both operational; a degraded plugin is
        still the best source of its own data.
        """
        return self.state in (PluginState.HEALTHY, PluginState.DEGRADED)

    # ------------------------------------------------------------------
    # Updates from discovery runs
    # ------------------------------------------------------------------

    def record_status(
        self,
        status: SourceStatus,
        *,
        message: str = "",
        timestamp: float | None = None,
    ) -> None:
        """Update health from the outcome of a ``discover()`` call.

        Escalation ladder for failures:
            1 .. threshold-1 consecutive failures  -> DEGRADED
            threshold or more                      -> UNAVAILABLE

        A ``SourceStatus.UNAVAILABLE`` result skips the ladder and goes
        straight to :attr:`PluginState.UNAVAILABLE`, because the plugin
        is explicitly reporting that it could not reach its source.

        Args:
            status: Status the plugin returned.
            message: Optional diagnostic detail (e.g. an error string).
            timestamp: Unix time of the update. Defaults to now.
        """
        self.last_checked_at = time.time() if timestamp is None else timestamp

        if self.state in PARKED_STATES:
            logger.info(
                "Plugin %s reported a run while %s; clearing parked state",
                self.plugin_name,
                self.state.value,
            )

        if status in HEALTHY_STATUSES:
            if self.consecutive_failures:
                logger.info(
                    "Plugin %s recovered after %d consecutive failure(s)",
                    self.plugin_name,
                    self.consecutive_failures,
                )
            self.consecutive_failures = 0
            self.state = PluginState.HEALTHY
            self.message = message or f"last run: {status.value}"
            return

        self.consecutive_failures += 1
        if status == SourceStatus.UNAVAILABLE:
            self.state = PluginState.UNAVAILABLE
        else:
            self.state = self._escalate()
        self.message = message or f"last run: {status.value}"
        logger.warning(
            "Plugin %s %s: status=%s consecutive_failures=%d (%s)",
            self.plugin_name,
            self.state.value,
            status.value,
            self.consecutive_failures,
            self.message,
        )

    def record_check(
        self,
        *,
        healthy: bool,
        message: str = "",
        details: dict[str, Any] | None = None,
        state: PluginState | str | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Update health from an explicit ``health_check()`` result.

        Args:
            healthy: Whether the plugin reported itself healthy.
            message: Human-readable diagnostic.
            details: Structured diagnostics returned by the plugin.
            state: Explicit state reported by the plugin, overriding the
                state that would be derived from ``healthy``. Lets a
                plugin distinguish DEGRADED or MAINTENANCE from a plain
                failure.
            timestamp: Unix time of the check. Defaults to now.
        """
        self.last_checked_at = time.time() if timestamp is None else timestamp
        self.details = details

        if state is not None:
            resolved = normalize_state(state)
            if resolved == PluginState.HEALTHY:
                self.consecutive_failures = 0
            elif resolved not in PARKED_STATES:
                self.consecutive_failures += 1
            self.state = resolved
            self.message = message or f"health check: {resolved.value}"
            return

        if healthy:
            self.consecutive_failures = 0
            self.state = PluginState.HEALTHY
            self.message = message or "health check passed"
            return

        self.consecutive_failures += 1
        self.state = self._escalate()
        self.message = message or "health check failed"
        logger.warning(
            "Plugin %s failed health check: %s (state=%s, "
            "consecutive_failures=%d)",
            self.plugin_name,
            self.message,
            self.state.value,
            self.consecutive_failures,
        )

    def _escalate(self) -> PluginState:
        """Pick DEGRADED or UNAVAILABLE from the failure count."""
        if self.consecutive_failures >= self.unavailable_threshold:
            return PluginState.UNAVAILABLE
        return PluginState.DEGRADED

    # ------------------------------------------------------------------
    # Explicit operator states
    # ------------------------------------------------------------------

    def mark_disabled(self, message: str = "") -> None:
        """Record that the plugin is switched off.

        Does not count a failure: not running is not being broken.
        """
        self.state = PluginState.DISABLED
        self.consecutive_failures = 0
        self.message = message or "plugin disabled"

    def mark_maintenance(self, message: str = "") -> None:
        """Record that the plugin is deliberately out of service."""
        self.state = PluginState.MAINTENANCE
        self.consecutive_failures = 0
        self.message = message or "plugin in maintenance"

    def mark_unknown(self, message: str = "") -> None:
        """Reset health to UNKNOWN without counting a failure.

        Retained for backward compatibility. Prefer
        :meth:`mark_disabled` or :meth:`mark_maintenance`, which say
        *why* there is no information.
        """
        self.state = PluginState.UNKNOWN
        self.consecutive_failures = 0
        self.message = message or "not yet checked"

    def reset(self) -> None:
        """Return health to its initial, never-checked state."""
        self.state = PluginState.UNKNOWN
        self.consecutive_failures = 0
        self.last_checked_at = None
        self.message = ""
        self.details = None

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize health for diagnostics and API output.

        Emits both the fine-grained ``state`` and the Phase 1
        ``status``, so new and old consumers are both served.
        """
        return {
            "plugin_name": self.plugin_name,
            "state": self.state.value,
            "status": self.status.value,
            "healthy": self.is_healthy,
            "degraded": self.is_degraded,
            "operational": self.is_operational,
            "consecutive_failures": self.consecutive_failures,
            "unavailable_threshold": self.unavailable_threshold,
            "last_checked_at": self.last_checked_at,
            "message": self.message,
            "details": self.details,
        }

    def __repr__(self) -> str:
        return (
            f"<PluginHealth plugin={self.plugin_name!r}"
            f" state={self.state.value}"
            f" status={self.status.value}"
            f" consecutive_failures={self.consecutive_failures}>"
        )
