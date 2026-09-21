"""Built-in buying-intent plugins (Increment 5).

Three free, keyless sources of buying-intent evidence for the lead
pipeline's intent stage:

- ``company_site`` — crawls the company's own website
  (``PROJECT_DISCOVERY``)
- ``google_news``  — Google News RSS search (``NEWS_DISCOVERY``)
- ``usaspending``  — USAspending.gov contract awards (``BID_DISCOVERY``)

All three extend :class:`BaseIntentPlugin`, so they register in the
shared plugin registry like any discovery plugin but are only ever
selected by capability — never by the company-discovery pipeline
(which filters on ``COMPANY_DISCOVERY``).

:func:`register_intent_plugins` is the startup seam: idempotent, so
calling it on every boot is safe (duplicates are skipped and logged by
the registry).

:func:`collect_intent_evidence` is the RUN seam — the single place the
plugins are actually driven for one company. It was extracted (Phase 1 of
the signal-intelligence engine) from ``LeadPipeline._collect_intent``,
which had the only copy; both callers now share this one so the
per-plugin error handling, the dedup rule and the honest per-provider
report cannot drift apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.discovery.intent.base import BaseIntentPlugin
from app.discovery.intent.company_site import CompanySiteIntentPlugin
from app.discovery.intent.google_news import GoogleNewsPlugin
from app.discovery.intent.usaspending import USAspendingPlugin
from app.discovery.plugins.plugin_registry import PluginRegistry, get_registry
from app.discovery.sources.status import SourceStatus
from app.engines.lead.lead_models import IntentEvidence

logger = logging.getLogger(__name__)

__all__ = [
    "BaseIntentPlugin",
    "CompanySiteIntentPlugin",
    "GoogleNewsPlugin",
    "IntentCollection",
    "USAspendingPlugin",
    "collect_intent_evidence",
    "default_intent_plugins",
    "registered_intent_plugins",
    "register_intent_plugins",
]


def default_intent_plugins() -> list[BaseIntentPlugin]:
    """The three free/keyless intent plugins, constructed offline-safe.

    Lives here rather than in a pipeline module because this package is
    what defines the plugins; ``app.engines.lead.lead_pipeline`` re-exports
    the name so its existing callers keep working unchanged.
    """
    return [
        CompanySiteIntentPlugin(),
        GoogleNewsPlugin(),
        USAspendingPlugin(),
    ]


def registered_intent_plugins(
    registry: PluginRegistry | None = None,
) -> list[BaseIntentPlugin]:
    """The ENABLED intent plugins currently registered, or ``[]``.

    Selected by ``isinstance``, not by capability: the discovery framework
    is open (a new plugin may declare any capability string), and an
    isinstance check cannot accidentally hand the research path a
    company-discovery plugin that happens to share a capability name.
    """
    target = registry if registry is not None else get_registry()
    try:
        enabled = target.get_enabled()
    except Exception:  # noqa: BLE001 — a broken registry is not fatal here
        logger.warning("intent plugin registry unavailable", exc_info=True)
        return []
    return [p for p in enabled if isinstance(p, BaseIntentPlugin)]


@dataclass(frozen=True)
class IntentCollection:
    """What one company-scoped intent run produced — and what it could not.

    ``providers`` is the honest per-provider record (CLAUDE.md §6: every
    execution must be diagnosable from the logs, never a bare "live=False").
    Each entry carries the provider name, its :class:`SourceStatus`, how
    many items it returned, how many survived the dedup rule, and its error
    or note when there is one.
    """

    evidence: list[IntentEvidence] = field(default_factory=list)
    providers: list[dict[str, Any]] = field(default_factory=list)
    plugins_available: list[str] = field(default_factory=list)

    @property
    def any_reachable(self) -> bool:
        """True when at least one provider ANSWERED (SUCCESS or EMPTY).

        The basis for separating "we searched and found nothing"
        (``NOT_FOUND``) from "we could not look" (``NOT_ACCESSIBLE``) —
        the two must never collapse (taxonomy ``ResearchState``). A
        provider that errored or was unavailable did not answer.
        """
        return any(
            entry.get("status") in (SourceStatus.SUCCESS.value, SourceStatus.EMPTY.value)
            for entry in self.providers
        )

    @property
    def unreachable(self) -> list[str]:
        """Names of providers that did NOT answer."""
        return [
            str(entry.get("provider", ""))
            for entry in self.providers
            if entry.get("status")
            not in (SourceStatus.SUCCESS.value, SourceStatus.EMPTY.value)
        ]

    def failure_reasons(self) -> dict[str, str]:
        """Provider name -> its ``SourceFailureReason``, where it gave one.

        Computed properties cannot reach a caller's log line, but the reason
        a provider failed decides what the failure MEANS: a rejected request
        is our own defect, while an unreachable host is nobody's fault yet.
        Carrying it here is what lets the intake's summary say which one
        happened instead of calling everything "unreachable".
        """
        return {
            str(entry.get("provider", "")): str(entry["reason"])
            for entry in self.providers
            if entry.get("reason")
        }


def collect_intent_evidence(
    plugins: Sequence[Any],
    *,
    company_name: str,
    website: str = "",
    location: str = "",
) -> IntentCollection:
    """Run every intent plugin for ONE company and report honestly.

    One plugin failing never kills the run (the existing behaviour, kept):
    its failure is recorded against its own name and the others continue.

    The dedup rule is by ``(type, source_url)`` and requires a NON-EMPTY
    ``source_url`` — a signal that cannot be traced to a URL is not
    evidence (lead schema hard rule #5).

    Args:
        plugins: The intent plugins to run (``default_intent_plugins()`` or
            :func:`registered_intent_plugins`).
        company_name: The company being evaluated.
        website: Its website, when known.
        location: Optional geographic context.

    Returns:
        An :class:`IntentCollection`. Never raises for a plugin failure.
    """
    evidence: list[IntentEvidence] = []
    seen: set[tuple[str, str]] = set()
    providers: list[dict[str, Any]] = []
    available = [getattr(p, "name", type(p).__name__) for p in plugins]

    for plugin in plugins:
        name = getattr(plugin, "name", type(plugin).__name__)
        entry: dict[str, Any] = {
            "provider": name,
            "status": SourceStatus.ERROR.value,
            "returned": 0,
            "accepted": 0,
            "deduped": 0,
            "blank_url": 0,
        }
        try:
            status, items, meta = plugin.collect_evidence(
                company_name=company_name,
                website=website,
                location=location,
            )
        except Exception as exc:  # noqa: BLE001 — one bad plugin never kills the run
            logger.warning("intent plugin %r failed: %s", name, exc)
            entry["error"] = f"{type(exc).__name__}: {exc}"
            providers.append(entry)
            continue

        entry["status"] = status.value if hasattr(status, "value") else str(status)
        entry["returned"] = len(items)
        # Carry the provider's own diagnosis through, not just its counts.
        # "Whose fault was this, and what did the source say?" is what turns a
        # thin result into something diagnosable without re-running anything —
        # and ``reason`` is what keeps a rejected request from being described
        # as an unreachable source further up the chain.
        if isinstance(meta, dict):
            for key in ("error", "reason", "detail", "note"):
                if meta.get(key):
                    entry[key] = str(meta[key])
        for item in items:
            url = (getattr(item, "source_url", "") or "").strip()
            if not url:
                entry["blank_url"] += 1
                continue
            key = (getattr(item.type, "value", str(item.type)), url)
            if key in seen:
                entry["deduped"] += 1
                continue
            seen.add(key)
            evidence.append(item)
            entry["accepted"] += 1
        providers.append(entry)

    return IntentCollection(
        evidence=evidence, providers=providers, plugins_available=available
    )


def register_intent_plugins(registry: PluginRegistry | None = None) -> list[str]:
    """Register the three built-in intent plugins, idempotently.

    Args:
        registry: Registry to register into. Defaults to the process-wide
            shared registry.

    Returns:
        Names of plugins newly registered. Duplicates are skipped by the
        registry (logged, never fatal), so a second call returns ``[]``.
    """
    target = registry if registry is not None else get_registry()
    plugins = default_intent_plugins()
    return [p.name for p in plugins if target.register(p)]
