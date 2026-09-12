"""The campaign scheduler — one slow, honest drain loop (Phase E3).

Runs as a daemon thread started by the app lifespan. Every pass:

    1. promote campaigns whose start_at has arrived (scheduled -> running)
    2. self-heal: 429 cooldowns expire, accounts that came back resume their
       campaigns (disconnect -> paused -> reconnect -> resume)
    3. for each RUNNING campaign, AT MOST ONE send:
         account healthy?  no  -> pause campaign (reason 'account')
         daily cap hit?    yes -> skip until tomorrow (campaign stays running)
         gap since last send elapsed?  no -> skip (random 3-7 min pacing)
         render -> refresh token if expired -> send via Gmail API
         429 -> pause + auto-resume after RATE_LIMIT_COOLDOWN_S
         401/403 -> mark account revoked + pause campaign
         other error -> retry later (attempts cap in the store)
         success -> mark sent + CRM event (stage 'contacted', first email)

One send per campaign per pass is deliberate: pacing lives BETWEEN passes,
so the thread can never machine-gun a queue even if the delay math is wrong.

All network I/O goes through app.email_accounts.google (monkeypatched in
tests); the clock and RNG are injectable so every branch is testable
without sleeping.
"""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.campaigns.store import CampaignStore
from app.campaigns.templates import context_for, render
from app.email_accounts import google
from app.email_accounts.store import EmailAccountStore
from app.lead_research.service import LeadResearchStore

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 20          # scheduler pass cadence
RATE_LIMIT_COOLDOWN_S = 3600   # a 429 backs off for an hour
MAX_ATTEMPTS = 3               # per-lead retries before 'failed'
TOKEN_EXPIRY_MARGIN_S = 60     # refresh this early, not mid-send


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_ts(ts: str) -> datetime | None:
    """ISO string -> aware datetime (naive treated as UTC); None if bad."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class CampaignScheduler:
    """Owns the drain loop. Constructed once by the app lifespan; tests
    construct their own with injected stores."""

    def __init__(
        self,
        store: CampaignStore,
        email_store: EmailAccountStore,
        lead_store: LeadResearchStore,
        *,
        clock: Callable[[], datetime] = _utcnow,
        rng: Callable[[int, int], int] = random.randint,
    ) -> None:
        self._store = store
        self._email_store = email_store
        self._lead_store = lead_store
        self._clock = clock
        self._rng = rng
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- Thread plumbing (lifespan) ------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="campaign-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("campaign scheduler started (interval %ss)", CHECK_INTERVAL_S)

    def stop(self) -> None:
        self._stop.set()

    def _run_forever(self) -> None:
        while not self._stop.wait(CHECK_INTERVAL_S):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — one bad pass must not kill the loop
                logger.exception("campaign scheduler pass failed — retrying next pass")

    # -- One pass --------------------------------------------------------

    def run_once(self) -> dict[str, int]:
        """One full pass; returns what happened (for logs + tests)."""
        now = self._clock()
        stats = {"promoted": 0, "resumed_rate_limited": 0,
                 "resumed_account": 0, "sent": 0, "paused": 0}

        for cid in self._store.promote_scheduled(_iso(now)):
            stats["promoted"] += 1
            logger.info("campaign %d reached start_at -> running", cid)
        for cid in self._store.resume_rate_limited(_iso(now)):
            stats["resumed_rate_limited"] += 1
            logger.info("campaign %d 429-cooldown over -> running", cid)

        # Accounts that are healthy again bring their paused campaigns back
        # (disconnect -> paused -> reconnect -> resume).
        healthy: set[int] = set()
        for account_id, user_id in self._store.account_paused_accounts():
            creds = self._email_store.get_credentials(account_id, user_id)
            accounts = self._email_store.list_for_user(user_id)
            status = next((a["status"] for a in accounts
                           if a["id"] == account_id), "")
            if (creds is not None and status == "connected"
                    and (creds["access_token"] or creds["refresh_token"])):
                healthy.add(account_id)
        if healthy:
            stats["resumed_account"] = self._store.resume_for_accounts(
                sorted(healthy))
            if stats["resumed_account"]:
                logger.info("%d campaign(s) resumed: account reconnected",
                            stats["resumed_account"])

        for c in self._store.running_campaigns():
            self._drain_one(c, now, stats)
        return stats

    # -- One campaign, at most one send ------------------------------------

    def _drain_one(self, c: dict[str, Any], now: datetime,
                   stats: dict[str, int]) -> None:
        # Account gone (disconnected) -> honest pause, auto-resume on return.
        creds = self._email_store.get_credentials(c["account_id"], c["user_id"])
        if creds is None:
            self._store.set_status(c["id"], status="paused",
                                   paused_reason="account")
            stats["paused"] += 1
            logger.warning("campaign %d paused: account disconnected", c["id"])
            return
        accounts = self._email_store.list_for_user(c["user_id"])
        status = next((a["status"] for a in accounts
                       if a["id"] == c["account_id"]), "")
        if status != "connected" or (not creds["access_token"] and not creds["refresh_token"]):
            self._store.set_status(c["id"], status="paused",
                                   paused_reason="account")
            stats["paused"] += 1
            logger.warning("campaign %d paused: account %s (status=%s)",
                           c["id"], creds["email"], status or "unknown")
            return

        # Daily cap — per ACCOUNT across all campaigns (Gmail's limit is
        # per account, not per campaign).
        today = _iso(now)[:10]
        if self._store.sent_today_for_account(c["account_id"], today) >= c["daily_limit"]:
            return  # cap hit — nothing more today, campaign stays running

        send = self._store.next_pending(c["id"])
        if send is None:
            self._store.mark_completed_if_drained(c["id"])
            return

        # Random pacing: the gap since this campaign's last send must have
        # elapsed (a fresh draw each pass — human-jitter by construction).
        last = parse_ts(self._store.last_sent_at(c["id"]))
        if last is not None:
            gap = self._rng(int(c["delay_min_s"]), int(c["delay_max_s"]))
            if (now - last).total_seconds() < gap:
                return

        # The lead's dossier is the ONLY source of template facts.
        dossier = self._lead_store.get(send["email"])
        if dossier is None:
            self._store.mark_failed(send["id"], error="lead dossier not found")
            self._store.mark_completed_if_drained(c["id"])
            return
        ctx = context_for(dossier)
        subject = render(c["subject"], ctx)
        body = render(c["body"], ctx)

        access_token = self._access_token(c, creds, now)
        if not access_token:
            self._email_store.mark_status(c["account_id"], c["user_id"], "revoked")
            self._store.set_status(c["id"], status="paused",
                                   paused_reason="account")
            stats["paused"] += 1
            logger.warning("campaign %d paused: token refresh failed for %s",
                           c["id"], creds["email"])
            return

        try:
            google.send_gmail(
                access_token, to=send["email"], subject=subject, body=body,
                from_email=creds["email"],
            )
        except Exception as exc:  # noqa: BLE001 — mapped below by cause
            self._on_send_error(c, send, exc, stats)
            return

        self._store.mark_sent(send["id"], subject=subject, sent_at=_iso(now))
        stats["sent"] += 1
        logger.info("campaign %d sent to %s", c["id"], send["email"])
        # CRM: the first outbound email moves the lead to 'contacted' and
        # every send lands on the immutable timeline.
        self._lead_store.set_crm(
            send["email"], status="contacted",
            note=f"email sent via campaign '{c['name']}': {subject}",
            user_id=c["user_id"], username="",
        )
        self._store.mark_completed_if_drained(c["id"])

    def _access_token(self, c: dict[str, Any], creds: dict[str, Any],
                      now: datetime) -> str:
        """A valid access token, refreshing it if expired. Empty string =
        refresh failed (caller pauses the campaign)."""
        expires = parse_ts(creds.get("token_expires_at") or "")
        fresh = expires is not None and expires > now + timedelta(
            seconds=TOKEN_EXPIRY_MARGIN_S)
        if creds["access_token"] and fresh:
            return creds["access_token"]
        if not creds["refresh_token"]:
            return ""
        try:
            tokens = google.refresh_access_token(creds["refresh_token"])
        except Exception:  # noqa: BLE001 — Google's error shape varies
            logger.warning("token refresh failed for account %s",
                           creds["email"])
            return ""
        new_access = tokens.get("access_token", "")
        if not new_access:
            return ""
        expires_at = _iso(now + timedelta(seconds=int(tokens.get("expires_in", 3600))))
        self._email_store.update_tokens(
            c["account_id"], c["user_id"], access_token=new_access,
            token_expires_at=expires_at,
        )
        return new_access

    def _on_send_error(self, c: dict[str, Any], send: dict[str, Any],
                       exc: Exception, stats: dict[str, int]) -> None:
        """Map a Gmail failure to the honest campaign/account action."""
        resp = getattr(exc, "response", None)
        code = resp.status_code if resp is not None else None
        if code == 429:
            # Rate limited: pause, cool down, self-resume (user's design).
            resume_at = _iso(self._clock() + timedelta(seconds=RATE_LIMIT_COOLDOWN_S))
            self._store.set_status(c["id"], status="paused",
                                   paused_reason="rate_limited",
                                   resume_at=resume_at)
            stats["paused"] += 1
            logger.warning("campaign %d: Gmail 429 -> paused, auto-resume at %s",
                           c["id"], resume_at)
            return
        if code in (401, 403):
            # Grant revoked / permission gone: account is unhealthy — pause
            # every campaign on it (they resume when the account reconnects).
            self._email_store.mark_status(c["account_id"], c["user_id"], "revoked")
            self._store.set_status(c["id"], status="paused",
                                   paused_reason="account")
            stats["paused"] += 1
            logger.warning("campaign %d: Gmail %s -> account marked revoked",
                           c["id"], code)
            return
        # Anything else: count the attempt, retry while budget remains.
        attempts = send.get("attempts", 0) + 1
        if attempts >= MAX_ATTEMPTS:
            self._store.mark_failed(send["id"], error=str(exc)[:300])
            logger.warning("campaign %d: send to %s failed for good: %s",
                           c["id"], send["email"], exc)
            self._store.mark_completed_if_drained(c["id"])
        else:
            logger.info("campaign %d: send to %s failed (%s), will retry",
                        c["id"], send["email"], exc)
            self._store.bump_attempts(send["id"])


# App-level singleton wiring (lifespan start/stop; tests never touch this).
_scheduler: CampaignScheduler | None = None


def get_scheduler() -> CampaignScheduler:
    """The process-wide scheduler, wired to the real stores."""
    global _scheduler
    if _scheduler is None:
        from app.email_accounts.store import get_email_store
        from app.campaigns.store import get_campaign_store
        from app.api.v1.leads import _store as lead_store
        _scheduler = CampaignScheduler(
            get_campaign_store(), get_email_store(), lead_store,
        )
    return _scheduler
