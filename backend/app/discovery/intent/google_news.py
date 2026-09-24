"""Google News RSS buying-intent plugin (NEWS_DISCOVERY).

A free, keyless search of Google News' RSS feed for the company name
(plus an optional location). Each matching news item becomes an
:class:`IntentEvidence` whose ``source_url`` is the article's feed link
(traceable per lead schema hard rule #5) and whose snippet is the
headline. ``requests`` is used directly — no API key, no extra
dependency. Failures (non-200, malformed XML, network errors) are
reported honestly as ``UNAVAILABLE`` or ``EMPTY``, never as fake news.
Offline tests monkeypatch ``requests``.

THE COMPANY CHECK (fixed 2026-09-21)
------------------------------------
Google News matches a quoted name loosely, so the feed returns articles
that merely MENTION the company somewhere in the body while the headline
names a different firm. Every item is now required to name the company in
the only text we keep — the headline — using the shared
:func:`~app.engines.verification.identity_verifier.name_on_page` rule.
Measured live: 42 of the 198 stored Turner rows (21%) failed that test.
The dropped count rides in ``metadata["items_off_company"]`` so the
suppression is visible rather than a quietly thinner result.
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
from app.engines.verification.identity_verifier import name_on_page, name_tokens

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

        evidence, off_company = self._parse_items(response.text, company_name)
        if not evidence:
            return SourceStatus.EMPTY, [], {
                "source": self.name,
                "query": query,
                # Never silently: a feed that returned items but none about
                # the company is a DIFFERENT fact from an empty feed, and the
                # count is what tells them apart.
                "items_off_company": off_company,
                "note": "returned items, none naming the company"
                if off_company
                else "",
            }
        return SourceStatus.SUCCESS, evidence, {
            "source": self.name,
            "query": query,
            "results": len(evidence),
            "items_off_company": off_company,
        }

    # -- RSS parsing ------------------------------------------------------

    def _parse_items(
        self, xml_text: str, company_name: str = ""
    ) -> tuple[list[IntentEvidence], int]:
        """Every valid ``<item>`` about *company_name*, and how many were not.

        The company check is the fix for a defect measured live 2026-09-21:
        of the 198 google_news rows stored for ``turnerconstruction.com``, 42
        (21%) did not name the company at all — "Jacobs wins role on $1.7B New
        York public health lab", "ENR 2026 Top 400 Contractors 1-100", "ENR
        Top 100 Green Design Firms". Google News matches loosely; a result
        that does not name the company is not evidence ABOUT the company, and
        the only quotation we store is the headline, so there is no text in
        the record that could ever tie it back.

        The rule is the shared one (:func:`name_on_page`), not a second
        approximation of it, so this plugin and the ``company_match``
        classifier agree on what "named" means.

        Returns:
            ``(items, off_company_count)``. The count is returned rather than
            logged and forgotten: the caller reports it, and a drop that
            cannot be seen is indistinguishable from a source that never had
            anything.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.warning("GoogleNewsPlugin: feed XML parse failed: %s", exc)
            return [], 0

        tokens = name_tokens(company_name) if company_name.strip() else []
        items: list[IntentEvidence] = []
        off_company = 0
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            if tokens and not name_on_page(company_name, tokens, title):
                off_company += 1
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
        return items, off_company
