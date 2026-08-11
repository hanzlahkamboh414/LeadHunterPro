"""Intent-plugin base: buying-intent evidence for a known company.

The lead pipeline's buying-intent stage needs a source of
:class:`IntentEvidence` — traceable proof that a company is actively
buying (projects, hiring, expansions, contract awards). Company-discovery
plugins answer "find me companies"; intent plugins answer a different
question for a company the pipeline ALREADY found: "is this company
buying right now?"

This base extends :class:`BaseDiscoveryPlugin` so intent plugins inherit
the framework's registration, capability, config, and health machinery
unchanged while declaring a NEW method shape for the intent stage. The
two methods deliberately do different work:

- ``discover()`` (from the framework, implemented here) is
  company-discovery. Intent plugins have nothing to return for it, so it
  reports ``EMPTY`` with an explanatory note — never a fabricated company
  list, never a misleading ``SUCCESS`` (CLAUDE.md §12).
- ``collect_evidence()`` (new) is the intent stage's entry point. It
  takes a known company and returns :class:`IntentEvidence` records, each
  traceable to a ``source_url`` (lead schema hard rule #5).

Capabilities keep the two worlds apart: intent plugins declare
``PROJECT_DISCOVERY`` / ``NEWS_DISCOVERY`` / ``BID_DISCOVERY``, never
``COMPANY_DISCOVERY``, so the company-discovery pipeline — which selects
plugins by capability (see
:func:`app.discovery.sources.plugin_source.attach_plugin_source`) — never
runs them.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from app.discovery.plugins.base_plugin import BaseDiscoveryPlugin
from app.discovery.sources.status import SourceStatus
from app.engines.lead.lead_models import IntentEvidence


class BaseIntentPlugin(BaseDiscoveryPlugin):
    """A plugin that collects buying-intent evidence for one company.

    Concrete plugins implement :meth:`collect_evidence`. Everything else
    (registration, capability filtering, config, health) comes from the
    discovery-plugin framework unchanged.
    """

    def discover(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        """Intent plugins are not company-discovery sources.

        Reporting ``EMPTY`` here (never ``SUCCESS``, never a company list)
        means a capability-less run that happens to select an intent plugin
        produces an honest "nothing to return", not fake companies.
        """
        return (
            SourceStatus.EMPTY,
            [],
            {
                "source": self.name,
                "note": "intent plugin; call collect_evidence(), not discover()",
            },
        )

    @abstractmethod
    def collect_evidence(
        self,
        *,
        company_name: str,
        website: str = "",
        location: str = "",
    ) -> tuple[SourceStatus, list[IntentEvidence], dict[str, Any]]:
        """Collect buying-intent evidence for one known company.

        Args:
            company_name: The company being evaluated (free text).
            website: The company's website, when known. An empty string is
                valid and means the plugin has no website to work from.
            location: Optional geographic context (city/state) that may
                sharpen a search.

        Returns:
            Tuple of ``(status, evidence, metadata)``.

            - ``SUCCESS``     — at least one evidence item, each with a
              traceable ``source_url``.
            - ``EMPTY``       — ran correctly, found no buying signal.
            - ``UNAVAILABLE`` — the source could not be reached; the
              distinction from ``EMPTY`` must never be collapsed
              (CLAUDE.md §12).
            - ``ERROR``       — unexpected failure; put the detail in
              ``metadata["error"]``.

            ``metadata`` should include ``source`` (this plugin's name) so
            downstream attribution never guesses.
        """
        ...
