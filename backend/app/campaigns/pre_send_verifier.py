"""Bounded campaign recipient preflight, separate from the sending thread.

The result proves only address shape and domain mail routing. It cannot prove
that an individual mailbox exists; a real hard bounce remains the final signal.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app.campaigns.store import CampaignStore
from app.email.email_cleaner import is_acceptable_email
from app.lead_research.service import LeadResearchStore, PendingLeadsStore

logger = logging.getLogger(__name__)
CHECK_INTERVAL_S = 30
BATCH_SIZE = 100
DNS_WORKERS = 4


def domain_mail_route(domain: str) -> tuple[str, str]:
    """Accept MX or RFC 5321 A/AAAA fallback; reject only NXDOMAIN/null MX."""
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.timeout = 1.5
    resolver.lifetime = 2.5
    try:
        answer = resolver.resolve(domain, "MX")
    except dns.resolver.NXDOMAIN:
        return "invalid", "domain does not exist"
    except dns.resolver.NoAnswer:
        # A domain without MX may still receive mail at its A/AAAA address.
        for record_type in ("A", "AAAA"):
            try:
                if resolver.resolve(domain, record_type):
                    return "ready", "address record mail fallback"
            except dns.resolver.NXDOMAIN:
                return "invalid", "domain does not exist"
            except Exception:  # timeout / no answer => try the other family
                pass
        return "hold", "no confirmed mail route"
    except Exception:
        return "hold", "DNS lookup unavailable"
    hosts = [str(record.exchange).rstrip(".") for record in answer]
    if hosts and all(not host for host in hosts):
        return "invalid", "domain publishes null MX"
    if hosts and all(host for host in hosts):
        return "ready", "MX record found"
    return "hold", "ambiguous MX records"


class CampaignEmailVerifier:
    def __init__(self, store: CampaignStore, lead_store: LeadResearchStore,
                 *, domain_check=domain_mail_route) -> None:
        self._store = store
        self._lead_store = lead_store
        self._domain_check = domain_check
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="campaign-email-verifier", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("campaign recipient verification pass failed")
            if self._stop.wait(CHECK_INTERVAL_S):
                break

    def status(self, email: str) -> str | None:
        return self._store.email_check_status(email)

    def run_once(self) -> dict[str, int]:
        retry_before = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        ready_before = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        emails = self._store.emails_to_verify(
            limit=BATCH_SIZE, retry_before=retry_before,
            ready_before=ready_before)
        stats = {"checked": 0, "ready": 0, "hold": 0, "invalid": 0}
        if not emails:
            return stats
        acceptable = [email for email in emails
                      if is_acceptable_email(email)
                      and not email.rpartition("@")[0].startswith("%")]
        domains = sorted({email.rpartition("@")[2] for email in acceptable})
        with ThreadPoolExecutor(max_workers=DNS_WORKERS) as pool:
            domain_results = dict(zip(domains, pool.map(self._domain_check, domains)))
        for email in emails:
            if not is_acceptable_email(email):
                status, reason = "invalid", "malformed or non-contact address"
            elif email.rpartition("@")[0].startswith("%"):
                status, reason = "hold", "suspicious leading percent in mailbox"
            else:
                status, reason = domain_results[email.rpartition("@")[2]]
            self._store.save_email_check(email, status, reason)
            stats["checked"] += 1
            stats[status] += 1
            if status == "invalid":
                try:
                    self._lead_store.delete(
                        email, reason="invalid_email", username="campaign-verifier")
                    PendingLeadsStore(db_path=self._lead_store._db_path).remove([email])
                    self._store.purge_recipient(email)
                except Exception:
                    logger.exception("invalid campaign recipient cleanup failed for %s", email)
        if stats["invalid"] or stats["hold"]:
            logger.info("campaign email preflight: %s", stats)
        return stats
