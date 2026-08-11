"""Google News RSS buying-intent plugin (NEWS_DISCOVERY).

A free, keyless search of Google News' RSS feed for the company name
(plus an optional location). Each matching news item becomes an
:class:`IntentEvidence` whose ``source_url`` is the article's feed link
(traceable per lead schema hard rule #5) and whose snippet is the
headline. ``requests`` is used directly — no API key, no extra
dependency. Failures (non-200, malformed XML, network errors) are
reported honestly as ``UNAVAILABLE`` or ``EMPTY``, never as fake news.
Offline tests monkeypatch ``requests``.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any

import requests

from app.discovery.intent.base import BaseIntentPlugin
from app.discovery.plugins.base_plugin import PluginCapability
from app.discovery.sources.status import SourceStatus
from app.engines.lead.lead_models import IntentEvidence, IntentEvidenceType

logger = logging.getLogger(__name__)

#: Free, keyless Google News RSS search endpoint.
RSS_URL = "https://news.google.com/rss/search"

#: Resolver/network failures that count as "unavailable". Captured at
#: import so a monkeypatched ``requests`` (offline tests) still resolves.
_NETWORK_ERRORS = (requests.RequestException, ValueError)

#: Seconds to wait for the feed before giving up.
DEFAULT_TIMEOUT = 15


class GoogleNewsPlugin(BaseIntentPlugin):
    """Search Google News' RSS feed for company growth/project news."""

    name = "google_news"
    description = "Google News RSS search for company growth and project news (free, keyless)"
    priority = 20
    capabilities = (PluginCapability.NEWS_DISCOVERY,)

    def collect_evidence(
        self,
        *,
        company_name: str,
        website: str = "",
        location: str = "",
    ) -> tuple[SourceStatus, list[IntentEvidence], dict[str, Any]]:
        """Fetch the RSS feed and turn each item into news evidence."""
        query = company_name.strip()
        if location and location.strip():
            query = f"{query} {location.strip()}"

        try:
            response = requests.get(
                RSS_URL,
                params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                timeout=DEFAULT_TIMEOUT,
            )
        except _NETWORK_ERRORS as exc:
            logger.warning("GoogleNewsPlugin: RSS request failed for %r: %s", query, exc)
            return SourceStatus.UNAVAILABLE, [], {
                "source": self.name,
                "query": query,
                "error": str(exc),
            }
        if response.status_code != 200:
            logger.warning(
                "GoogleNewsPlugin: RSS non-200 for %r: %s",
                query,
                response.status_code,
            )
            return SourceStatus.UNAVAILABLE, [], {
                "source": self.name,
                "query": query,
                "status": response.status_code,
            }

        evidence = self._parse_items(response.text)
        if not evidence:
            return SourceStatus.EMPTY, [], {"source": self.name, "query": query}
        return SourceStatus.SUCCESS, evidence, {
            "source": self.name,
            "query": query,
            "results": len(evidence),
        }

    # -- RSS parsing ------------------------------------------------------

    def _parse_items(self, xml_text: str) -> list[IntentEvidence]:
        """Every valid ``<item>`` in the feed, as news evidence."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.warning("GoogleNewsPlugin: feed XML parse failed: %s", exc)
            return []

        items: list[IntentEvidence] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            items.append(
                IntentEvidence(
                    type=IntentEvidenceType.news,
                    source_url=link,
                    snippet=title,
                    date=(item.findtext("pubDate") or "").strip(),
                    source="google_news",
                )
            )
        return items
