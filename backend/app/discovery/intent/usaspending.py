"""USAspending.gov buying-intent plugin (BID_DISCOVERY).

Searches the free, keyless USAspending.gov API for federal contract
awards to the company. Each award becomes an :class:`IntentEvidence` of
type ``bid_award`` whose ``source_url`` is the award's public
USAspending page — traceable per lead schema hard rule #5. ``requests``
is used directly; no API key, no extra dependency. Failures (non-200,
network errors) are ``UNAVAILABLE``, never a fabricated award. Offline
tests monkeypatch ``requests``.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from app.discovery.intent.base import BaseIntentPlugin
from app.discovery.plugins.base_plugin import PluginCapability
from app.discovery.sources.status import SourceStatus
from app.engines.lead.lead_models import IntentEvidence, IntentEvidenceType

logger = logging.getLogger(__name__)

#: Free, keyless USAspending award-search endpoint.
SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

#: Public page for one award; traceable source for the evidence.
AWARD_PAGE_URL = "https://www.usaspending.gov/award/{award_id}"

#: How far back contract awards are considered buying intent.
START_DATE = "2019-10-01"
END_DATE = "2026-09-30"

#: Award fields requested from the API.
FIELDS = (
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Awarding Agency",
    "Start Date",
    "End Date",
    "Description",
)

#: Network failures that count as "unavailable". Captured at import so a
#: monkeypatched ``requests`` (offline tests) still resolves the tuple.
_NETWORK_ERRORS = (requests.RequestException, ValueError)

#: Seconds to wait for the API before giving up.
DEFAULT_TIMEOUT = 30


class USAspendingPlugin(BaseIntentPlugin):
    """Search USAspending.gov for federal contract awards to the company."""

    name = "usaspending"
    description = "USAspending.gov API search for federal contract awards (free, keyless)"
    priority = 30
    capabilities = (PluginCapability.BID_DISCOVERY,)

    def collect_evidence(
        self,
        *,
        company_name: str,
        website: str = "",
        location: str = "",
    ) -> tuple[SourceStatus, list[IntentEvidence], dict[str, Any]]:
        """Query the award search API and turn each award into evidence."""
        payload = {
            "filters": {
                "recipient_search_text": [company_name.strip()],
                "time_period": [{"start_date": START_DATE, "end_date": END_DATE}],
            },
            "fields": list(FIELDS),
            "limit": 20,
        }

        try:
            response = requests.post(SEARCH_URL, json=payload, timeout=DEFAULT_TIMEOUT)
        except _NETWORK_ERRORS as exc:
            logger.warning(
                "USAspendingPlugin: API request failed for %r: %s",
                company_name,
                exc,
            )
            return SourceStatus.UNAVAILABLE, [], {
                "source": self.name,
                "error": str(exc),
            }
        if response.status_code != 200:
            logger.warning(
                "USAspendingPlugin: API non-200 for %r: %s",
                company_name,
                response.status_code,
            )
            return SourceStatus.UNAVAILABLE, [], {
                "source": self.name,
                "status": response.status_code,
            }

        try:
            data = response.json()
        except _NETWORK_ERRORS as exc:
            logger.warning(
                "USAspendingPlugin: API returned unparsable JSON for %r: %s",
                company_name,
                exc,
            )
            return SourceStatus.UNAVAILABLE, [], {
                "source": self.name,
                "error": str(exc),
            }

        evidence = [self._to_evidence(award) for award in (data.get("results") or [])]
        evidence = [e for e in evidence if e is not None]
        if not evidence:
            return SourceStatus.EMPTY, [], {"source": self.name, "query": company_name}
        return SourceStatus.SUCCESS, evidence, {
            "source": self.name,
            "query": company_name,
            "results": len(evidence),
        }

    # -- award mapping ----------------------------------------------------

    @staticmethod
    def _to_evidence(award: dict[str, Any]) -> IntentEvidence | None:
        """One award dict -> traceable bid_award evidence, or None."""
        award_id = str(award.get("Award ID") or "").strip()
        if not award_id:
            return None  # without an ID there is no traceable source_url

        parts: list[str] = []
        recipient = award.get("Recipient Name")
        if recipient:
            parts.append(str(recipient))
        amount = award.get("Award Amount")
        if amount is not None:
            parts.append(USAspendingPlugin._format_amount(amount))
        agency = award.get("Awarding Agency")
        if agency:
            parts.append(str(agency))

        date = award.get("Start Date") or award.get("End Date") or ""
        return IntentEvidence(
            type=IntentEvidenceType.bid_award,
            source_url=AWARD_PAGE_URL.format(award_id=award_id),
            snippet=" — ".join(parts),
            date=str(date) if date else "",
            source="usaspending",
        )

    @staticmethod
    def _format_amount(amount: Any) -> str:
        """Render an award amount as dollars, whatever the API sent."""
        if isinstance(amount, bool):
            return str(amount)
        if isinstance(amount, (int, float)):
            return f"${amount:,.0f}"
        return str(amount)
