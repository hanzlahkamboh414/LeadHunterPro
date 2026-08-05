"""Base interface for all discovery plugins.

A *plugin* is the unit of discovery capability in Phase 2. Every future
discovery source — government registries, trade directories, website
crawlers, search providers, enrichment services — implements this one
interface and nothing more. That is what makes them interchangeable:
the framework depends on this contract, never on a concrete provider
(CLAUDE.md §4).

The contract intentionally mirrors the Phase 1
:class:`~app.discovery.sources.base_source.BaseSource` interface:

    ``discover()``      synchronous, returns
                        ``(SourceStatus, companies, metadata)``
    ``health_check()``  asynchronous, returns a diagnostic dict

Keeping the shapes identical means a plugin can be adapted to the
existing :class:`~app.discovery.source_orchestrator.SourceOrchestrator`
later without either side changing — the orchestrator is untouched by
this phase.

What a plugin owns:
    Fetching from its own source, and reporting an honest status.

What a plugin must NOT do:
    Deduplicate across sources, rank, apply fixture fallback, or reach
    into another plugin. Those are pipeline concerns and live above
    the plugin layer.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable
from enum import Enum
from typing import Any

from app.discovery.plugins.plugin_config import PluginConfig
from app.discovery.sources.status import SourceStatus

logger = logging.getLogger(__name__)


class PluginCapability(str, Enum):
    """What a plugin is able to produce.

    Capabilities let the pipeline select plugins by what they *do*
    rather than by name, so a stage can ask for "everything that can
    find leadership" without hardcoding a provider list.

    **Open by design.** The members below are the known capabilities,
    but this enum is not a closed whitelist. Because it subclasses
    ``str``, a plugin may declare a capability the framework has never
    heard of::

        class PatentPlugin(BaseDiscoveryPlugin):
            capabilities = ("patent_discovery",)

    Every framework comparison normalizes through
    :func:`normalize_capability`, so an unknown capability filters,
    matches, and serializes exactly like a declared one. Adding a
    discovery type therefore requires **no framework modification** —
    promoting a string to a member here is an optional readability
    step, not a prerequisite. This is what keeps the framework
    provider-agnostic (CLAUDE.md §4).
    """

    #: Finds companies (name, location, and ideally a website).
    COMPANY_DISCOVERY = "company_discovery"
    #: Finds decision makers, executives, and owners.
    LEADERSHIP_DISCOVERY = "leadership_discovery"
    #: Finds email addresses.
    EMAIL_DISCOVERY = "email_discovery"
    #: Finds phone numbers.
    PHONE_DISCOVERY = "phone_discovery"
    #: Resolves a company to its official website / domain.
    WEBSITE_DISCOVERY = "website_discovery"
    #: Finds social media profiles.
    SOCIAL_DISCOVERY = "social_discovery"
    #: Finds projects a company is working on.
    PROJECT_DISCOVERY = "project_discovery"
    #: Finds tenders, bids, and contract awards.
    BID_DISCOVERY = "bid_discovery"
    #: Finds building or trade permits.
    PERMIT_DISCOVERY = "permit_discovery"
    #: Finds news, press releases, and announcements.
    NEWS_DISCOVERY = "news_discovery"

    def __str__(self) -> str:
        """Render as the bare value, not ``PluginCapability.X``."""
        return self.value


def normalize_capability(capability: PluginCapability | str) -> str:
    """Reduce a capability to its canonical string form.

    The single choke point through which every capability comparison in
    the framework passes. Accepting both the enum member and its raw
    string is what allows a future plugin to declare an unknown
    capability without the framework changing.

    Args:
        capability: Enum member, or a raw capability string.

    Returns:
        The lowercase canonical string value.

    Raises:
        TypeError: If the value is neither an enum member nor a string.
    """
    if isinstance(capability, PluginCapability):
        return capability.value
    if isinstance(capability, str):
        return capability.strip().lower()
    raise TypeError(
        f"Capability must be a PluginCapability or str, "
        f"got {type(capability).__name__}"
    )


class BaseDiscoveryPlugin(ABC):
    """Abstract base class every discovery plugin must implement.

    Subclasses declare their identity as class attributes and implement
    :meth:`discover`. Everything else has a working default.

    Attributes:
        name: Unique plugin identifier (e.g. ``"texas_sos"``).
        description: Human-readable one-liner.
        priority: Execution order. Lower numbers run first.
        enabled: Whether the plugin participates in discovery runs.
        capabilities: What this plugin can produce. Enum members or
            raw strings; both are treated identically.

    Example:
        class MyPlugin(BaseDiscoveryPlugin):
            name = "my_plugin"
            priority = 20
            capabilities = (PluginCapability.COMPANY_DISCOVERY,)

            def discover(self, *, industry, location, limit):
                companies = self._fetch(industry, location, limit)
                if not companies:
                    return SourceStatus.EMPTY, [], {"query": industry}
                return SourceStatus.SUCCESS, companies, {"query": industry}
    """

    name: str = ""
    description: str = ""
    priority: int = 100
    enabled: bool = True
    capabilities: tuple[PluginCapability | str, ...] = ()

    def __init__(self, config: PluginConfig | None = None) -> None:
        """Initialize the plugin.

        When a config is supplied it is authoritative: ``enabled`` and
        ``priority`` are taken from it, so runtime configuration always
        beats the class-level defaults. When no config is supplied one
        is synthesized from the class attributes, meaning every plugin
        always has a config and callers never branch on ``None``.

        Args:
            config: Optional runtime configuration.

        Raises:
            ValueError: If the subclass did not declare a ``name``.
        """
        if not self.name or not self.name.strip():
            raise ValueError(
                f"{type(self).__name__} must declare a non-empty 'name' "
                f"class attribute"
            )

        if config is None:
            config = PluginConfig(
                name=self.name,
                enabled=self.enabled,
                priority=self.priority,
            )
        elif config.name != self.name:
            # Not fatal, but a config bound to the wrong plugin is almost
            # always a wiring bug and must never be swallowed silently.
            logger.warning(
                "PluginConfig name mismatch: config.name=%r but plugin.name=%r",
                config.name,
                self.name,
            )

        self.config = config
        self.enabled = config.enabled
        self.priority = config.priority

    @abstractmethod
    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        """Execute discovery for this plugin.

        Implementations must return a status honestly. Returning
        ``SUCCESS`` with an empty list, or swallowing an exception and
        returning ``EMPTY``, hides failures from the pipeline and is a
        contract violation.

        Args:
            industry: Industry keyword (e.g. ``"Roofing"``).
            location: Geographic location (e.g. ``"Dallas Texas"``).
            limit: Maximum results to return.

        Returns:
            Tuple of ``(status, companies, metadata)``.

            - ``SUCCESS``     — found companies, list is non-empty.
            - ``EMPTY``       — ran correctly, found nothing.
            - ``UNAVAILABLE`` — could not reach the source
              (DNS failure, timeout, connection refused).
            - ``ERROR``       — unexpected exception; put the detail in
              ``metadata["error"]``.

            ``metadata`` may include ``duplicates_removed`` when the
            plugin filtered duplicates *within its own* result set. The
            framework records that number; cross-plugin deduplication
            remains the orchestrator's job.
        """
        ...

    async def health_check(self) -> dict[str, Any]:
        """Lightweight liveness check.

        The default implementation reports configuration state only —
        it performs no I/O. Plugins that talk to a remote source should
        override this to probe that source.

        Returns:
            Dict with ``healthy`` (bool), ``plugin``, ``status``, and
            any plugin-specific diagnostics. May also include a
            ``state`` key naming a :class:`PluginState` value (for
            example ``"degraded"`` or ``"maintenance"``) when the
            plugin can report something more precise than a boolean.
        """
        return {
            "healthy": self.enabled,
            "plugin": self.name,
            "status": "active" if self.enabled else "disabled",
        }

    @property
    def normalized_capabilities(self) -> tuple[str, ...]:
        """Declared capabilities in canonical string form."""
        return tuple(normalize_capability(c) for c in self.capabilities)

    def supports(self, capability: PluginCapability | str) -> bool:
        """Whether this plugin declares the given capability."""
        return normalize_capability(capability) in self.normalized_capabilities

    def supports_any(
        self,
        capabilities: Iterable[PluginCapability | str],
    ) -> bool:
        """Whether this plugin declares at least one of the capabilities."""
        declared = self.normalized_capabilities
        return any(normalize_capability(c) in declared for c in capabilities)

    def supports_all(
        self,
        capabilities: Iterable[PluginCapability | str],
    ) -> bool:
        """Whether this plugin declares every one of the capabilities."""
        declared = self.normalized_capabilities
        return all(normalize_capability(c) in declared for c in capabilities)

    def describe(self) -> dict[str, Any]:
        """Serialize the plugin's identity for diagnostics and API output."""
        return {
            "name": self.name,
            "description": self.description,
            "priority": self.priority,
            "enabled": self.enabled,
            "capabilities": list(self.normalized_capabilities),
            "class": type(self).__name__,
        }

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__}"
            f" name={self.name!r}"
            f" priority={self.priority}"
            f" enabled={self.enabled}"
            f" capabilities={list(self.normalized_capabilities)}>"
        )
