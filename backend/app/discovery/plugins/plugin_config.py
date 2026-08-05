"""Plugin configuration.

Every discovery plugin is configured through a :class:`PluginConfig`
value object rather than through constructor keyword soup or module
level globals. This keeps plugin behaviour declarative and inspectable:
the orchestrating layer can read a plugin's configuration without
importing or instantiating the plugin's dependencies.

Configuration is deliberately provider-agnostic. It carries only the
knobs the framework itself needs; anything provider-specific — API
keys, endpoints, query templates — lives in
:attr:`PluginConfig.options`, so adding a new plugin never requires
changing this module.

Execution-policy fields (``retry_count``, ``rate_limit``, ``cache_ttl``)
are **declared here but not enforced here**. Enforcement will bind to
the engines that already exist in this repository
(:class:`app.engines.source_connectors.retry_manager.RetryManager`,
:class:`app.engines.source_connectors.rate_limiter.RateLimiter`) rather
than growing a second implementation inside the plugin framework
(CLAUDE.md §14). Declaring the knob now keeps the plugin contract
stable when that wiring lands.

Backward compatibility:
    ``timeout_seconds`` was the original field name for ``timeout``.
    It remains supported as a constructor keyword, as an attribute
    read, and as a key in both :meth:`to_dict` and :meth:`from_dict`.
"""

from __future__ import annotations

import logging
from dataclasses import InitVar, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Default per-plugin execution timeout in seconds.
DEFAULT_TIMEOUT_SECONDS: float = 30.0

#: Default per-plugin result cap.
DEFAULT_MAX_RESULTS: int = 50

#: Default execution priority (lower runs first).
DEFAULT_PRIORITY: int = 100

#: Default scoring weight applied to this plugin's results.
DEFAULT_WEIGHT: float = 1.0

#: Default number of retries after the initial attempt.
DEFAULT_RETRY_COUNT: int = 2

#: Default rate limit in requests per second. ``0`` disables limiting.
DEFAULT_RATE_LIMIT: float = 0.0

#: Default cache lifetime in seconds. ``0`` disables caching.
DEFAULT_CACHE_TTL: int = 0


@dataclass
class PluginConfig:
    """Declarative configuration for a single discovery plugin.

    Attributes:
        name: Plugin identifier. Must match the plugin's ``name``.
        enabled: Whether the plugin participates in discovery runs.
        priority: Execution order. Lower numbers run first.
        weight: Relative confidence in this plugin's results. Consumed
            by the ranking stage, which lives above the framework.
        retry_count: Retries after the initial attempt. Declared for
            the retry engine; not enforced by the framework.
        timeout: Execution budget in seconds. The framework records it
            and exposes it to the plugin; it does not forcibly kill
            work, because plugins own their own transport layer.
        rate_limit: Maximum requests per second. ``0`` means unlimited.
            Declared for the rate limiter; not enforced here.
        cache_ttl: Result cache lifetime in seconds. ``0`` disables.
        max_results: Upper bound on results this plugin should return.
        supports_parallel: Plugin is safe to run concurrently with
            others. Consumed by a future concurrent executor; the
            current executor is sequential and ignores it.
        supports_batch: Plugin can accept several queries in one call.
        options: Free-form, plugin-specific settings. The framework
            never interprets these.
        timeout_seconds: Deprecated alias for ``timeout``, accepted as
            a constructor keyword for backward compatibility.
    """

    name: str
    enabled: bool = True
    priority: int = DEFAULT_PRIORITY
    weight: float = DEFAULT_WEIGHT
    retry_count: int = DEFAULT_RETRY_COUNT
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    rate_limit: float = DEFAULT_RATE_LIMIT
    cache_ttl: int = DEFAULT_CACHE_TTL
    max_results: int = DEFAULT_MAX_RESULTS
    supports_parallel: bool = False
    supports_batch: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    #: Legacy constructor keyword. Not stored as a field; folded into
    #: ``timeout`` by ``__post_init__`` so the two can never drift.
    timeout_seconds: InitVar[float | None] = None

    def __post_init__(self, timeout_seconds: float | None = None) -> None:
        """Fold the legacy alias in, then validate.

        Failing loudly here prevents a misconfigured plugin from being
        registered and then silently producing nothing at runtime.
        """
        if timeout_seconds is not None:
            self.timeout = timeout_seconds

        if not self.name or not self.name.strip():
            raise ValueError("PluginConfig.name must be a non-empty string")
        if self.timeout <= 0:
            raise ValueError(
                f"PluginConfig.timeout must be > 0 "
                f"(got {self.timeout!r} for {self.name!r})"
            )
        if self.max_results < 0:
            raise ValueError(
                f"PluginConfig.max_results must be >= 0 "
                f"(got {self.max_results!r} for {self.name!r})"
            )
        if self.retry_count < 0:
            raise ValueError(
                f"PluginConfig.retry_count must be >= 0 "
                f"(got {self.retry_count!r} for {self.name!r})"
            )
        if self.rate_limit < 0:
            raise ValueError(
                f"PluginConfig.rate_limit must be >= 0 "
                f"(got {self.rate_limit!r} for {self.name!r})"
            )
        if self.cache_ttl < 0:
            raise ValueError(
                f"PluginConfig.cache_ttl must be >= 0 "
                f"(got {self.cache_ttl!r} for {self.name!r})"
            )
        if self.weight < 0:
            raise ValueError(
                f"PluginConfig.weight must be >= 0 "
                f"(got {self.weight!r} for {self.name!r})"
            )

    def get(self, key: str, default: Any = None) -> Any:
        """Read a plugin-specific option.

        Args:
            key: Option name.
            default: Value returned when the option is absent.

        Returns:
            The option value, or ``default``.
        """
        return self.options.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the configuration for diagnostics and API output.

        Emits both ``timeout`` and the legacy ``timeout_seconds`` key so
        that older consumers keep working and a round-trip through
        :meth:`from_dict` is lossless in either direction.
        """
        return {
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "weight": self.weight,
            "retry_count": self.retry_count,
            "timeout": self.timeout,
            # Deprecated alias, kept for backward compatibility.
            "timeout_seconds": self.timeout,
            "rate_limit": self.rate_limit,
            "cache_ttl": self.cache_ttl,
            "max_results": self.max_results,
            "supports_parallel": self.supports_parallel,
            "supports_batch": self.supports_batch,
            "options": dict(self.options),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginConfig:
        """Build a config from a plain dict (e.g. settings or JSON).

        Unknown keys are collected into :attr:`options` instead of
        raising, so a plugin can ship new settings without the
        framework needing a code change.

        Args:
            data: Mapping containing at least ``name``.

        Returns:
            A validated :class:`PluginConfig`.
        """
        known = {
            "name",
            "enabled",
            "priority",
            "weight",
            "retry_count",
            "timeout",
            "timeout_seconds",
            "rate_limit",
            "cache_ttl",
            "max_results",
            "supports_parallel",
            "supports_batch",
            "options",
        }
        options = dict(data.get("options") or {})
        for key, value in data.items():
            if key not in known:
                options[key] = value

        # Explicit `timeout` wins over the legacy alias when both appear.
        if "timeout" in data:
            timeout = float(data["timeout"])
        elif "timeout_seconds" in data:
            timeout = float(data["timeout_seconds"])
        else:
            timeout = DEFAULT_TIMEOUT_SECONDS

        return cls(
            name=data["name"],
            enabled=bool(data.get("enabled", True)),
            priority=int(data.get("priority", DEFAULT_PRIORITY)),
            weight=float(data.get("weight", DEFAULT_WEIGHT)),
            retry_count=int(data.get("retry_count", DEFAULT_RETRY_COUNT)),
            timeout=timeout,
            rate_limit=float(data.get("rate_limit", DEFAULT_RATE_LIMIT)),
            cache_ttl=int(data.get("cache_ttl", DEFAULT_CACHE_TTL)),
            max_results=int(data.get("max_results", DEFAULT_MAX_RESULTS)),
            supports_parallel=bool(data.get("supports_parallel", False)),
            supports_batch=bool(data.get("supports_batch", False)),
            options=options,
        )

    def __repr__(self) -> str:
        return (
            f"<PluginConfig name={self.name!r}"
            f" enabled={self.enabled}"
            f" priority={self.priority}"
            f" weight={self.weight}"
            f" timeout={self.timeout}s"
            f" retries={self.retry_count}"
            f" max_results={self.max_results}>"
        )


# `timeout_seconds` is an InitVar, so @dataclass leaves its default (None)
# behind as a real class attribute. That would shadow any attribute read and
# make `config.timeout_seconds` return None instead of the timeout. Replace it
# with a read-only alias so the deprecated name can never drift from `timeout`.
# The constructor keyword keeps working — InitVar defaults are baked into the
# generated __init__ signature, not looked up on the class at call time.
PluginConfig.timeout_seconds = property(
    lambda self: self.timeout,
    doc="Deprecated read-only alias for :attr:`PluginConfig.timeout`.",
)
