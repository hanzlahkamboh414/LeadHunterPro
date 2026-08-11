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
"""

from __future__ import annotations

from app.discovery.intent.base import BaseIntentPlugin
from app.discovery.intent.company_site import CompanySiteIntentPlugin
from app.discovery.intent.google_news import GoogleNewsPlugin
from app.discovery.intent.usaspending import USAspendingPlugin
from app.discovery.plugins.plugin_registry import PluginRegistry, get_registry

__all__ = [
    "BaseIntentPlugin",
    "CompanySiteIntentPlugin",
    "GoogleNewsPlugin",
    "USAspendingPlugin",
    "register_intent_plugins",
]


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
    plugins = (CompanySiteIntentPlugin(), GoogleNewsPlugin(), USAspendingPlugin())
    return [p.name for p in plugins if target.register(p)]
