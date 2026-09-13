"""Phone-lead enrichment worker — the phones vertical's background lane.

One daemon thread (started by the app lifespan, NEVER at import time so
pytest never spawns a real crawl loop — the campaign-scheduler pattern).
Each pass:

1. picks a small batch of CLAIMED-but-unenriched phone leads (a lead
   nobody owns has nobody waiting on its email),
2. enriches each via :func:`app.phones.enrich.enrich_lead` (web search for
   the official site + crawl for emails; when the crawl finds none, the P5
   pattern-inference stage asks the company's mail server — free, no AI),
3. stamps the outcome on the lead (found email, or the honest miss), and
4. feeds every FOUND email into the emails vertical's pending-leads cache
   so email-vertical users are served it through their normal research
   pipeline (the phone lead's owner still sees it on their own lead —
   cross-vertical by value, the direction phones -> emails only).

A lead that hard-errors enrich stays pending for a later pass — but a
poison lead must not starve the batch head, so after ``_MAX_FAILURES``
consecutive errors (per process) it is skipped until restart.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from app.lead_research.service import PendingLeadsStore
from app.phones.enrich import enrich_lead
from app.phones.store import PhoneLeadsStore

logger = logging.getLogger(__name__)

#: How often the worker wakes up and looks for work.
INTERVAL_S = 15.0
#: How many leads one pass may enrich (gentle on the crawl + search rate).
BATCH_SIZE = 5
#: Consecutive hard errors before a lead is set aside for this process.
_MAX_FAILURES = 3


class PhoneEnrichmentWorker:
    """Owns the enrichment loop. Constructed once by the app lifespan;
    tests construct their own with injected stores and a fake enricher."""

    def __init__(
        self,
        phone_store: PhoneLeadsStore,
        pending_store: PendingLeadsStore,
        *,
        enrich: Callable[[dict[str, Any]], dict[str, str]] | None = None,
        interval_s: float = INTERVAL_S,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._phone_store = phone_store
        self._pending_store = pending_store
        self._enrich = enrich or enrich_lead
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._fail_counts: dict[int, int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="phone-enricher", daemon=True,
        )
        self._thread.start()
        logger.info(
            "phone enrichment worker started (interval %ss, batch %d)",
            self._interval_s, self._batch_size,
        )

    def stop(self) -> None:
        self._stop.set()

    def _run_forever(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — one bad pass must not kill the loop
                logger.exception(
                    "phone enrichment pass failed — retrying next pass"
                )

    # -- one pass ---------------------------------------------------------------

    def run_once(self) -> dict[str, int]:
        """One enrichment pass; returns what happened (logs + tests)."""
        stats = {
            "considered": 0, "found": 0, "no_email": 0,
            "fed_to_emails": 0, "errors": 0, "skipped_poison": 0,
        }
        leads = self._phone_store.pending_enrichment(
            self._batch_size * 2,  # headroom: skipped poison leads don't shrink the batch
        )
        for lead in leads:
            if stats["found"] + stats["no_email"] + stats["errors"] >= self._batch_size:
                break
            if self._fail_counts.get(lead["id"], 0) >= _MAX_FAILURES:
                stats["skipped_poison"] += 1
                continue
            stats["considered"] += 1
            try:
                outcome = self._enrich(lead)
            except Exception:  # noqa: BLE001 — one bad lead must not kill the pass
                stats["errors"] += 1
                self._fail_counts[lead["id"]] = (
                    self._fail_counts.get(lead["id"], 0) + 1
                )
                logger.exception(
                    "enriching lead %s (%s) failed (%d/%d)",
                    lead["id"], lead.get("business_name", ""),
                    self._fail_counts[lead["id"]], _MAX_FAILURES,
                )
                continue

            self._phone_store.set_enrichment(
                lead["id"],
                email=outcome.get("email", ""),
                email_source=outcome.get("email_source", ""),
                website=outcome.get("website", ""),
            )
            if outcome.get("email"):
                stats["found"] += 1
                stats["fed_to_emails"] += self._feed_email_vertical(lead, outcome)
            else:
                stats["no_email"] += 1

        if stats["considered"] or stats["skipped_poison"]:
            logger.info("phone enrichment pass: %s", stats)
        return stats

    def _feed_email_vertical(
        self, lead: dict[str, Any], outcome: dict[str, str],
    ) -> int:
        """Stock the found email into the emails vertical's pending cache.

        The pending intake applies its own guards (free-mail drop, non-client
        gate) — a gmail contact address shows on the phone lead for the
        calling user but is honestly NOT stocked as an emails-vertical lead.
        """
        email = outcome.get("email", "")
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        location = ", ".join(
            x for x in (lead.get("city", ""), lead.get("state", "")) if x
        )
        return self._pending_store.add([{
            "email": email,
            "domain": domain,
            "company": lead.get("business_name", ""),
            "person": lead.get("person_name", ""),
            "source_url": outcome.get("website", "") or lead.get("source_url", ""),
            "location": location,
            # The lane tag: "overture" (P8 — the phone-keyed dataset join),
            # "phone_enrichment" (literally seen on the site) or
            # "pattern_inference" (P5 — mail-server-confirmed permutation).
            "dork": outcome.get("dork", "") or "phone_enrichment",
            "trade": lead.get("trade", ""),
        }])


_worker: PhoneEnrichmentWorker | None = None
_worker_lock = threading.Lock()


def get_worker() -> PhoneEnrichmentWorker:
    """Process-wide worker bound to the real stores (lifespan starts it)."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = PhoneEnrichmentWorker(
                PhoneLeadsStore(), PendingLeadsStore(),
            )
        return _worker
