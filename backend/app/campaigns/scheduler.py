"""The campaign scheduler — one slow, honest drain loop (Phase E3/E4).

Runs as a daemon thread started by the app lifespan. Every pass:

    1. promote campaigns whose start_at has arrived (scheduled -> running)
    2. self-heal: 429 cooldowns expire, accounts that came back resume their
       campaigns (disconnect -> paused -> reconnect -> resume)
    3. reply detection (throttled to REPLY_CHECK_INTERVAL_S per account):
       read the connected inbox's recent senders (metadata only) and match
       them against sent-but-unreplied leads — a reply cancels that lead's
       pending follow-ups and moves their CRM stage to 'replied'
    4. for each RUNNING campaign, AT MOST ONE send:
         account healthy?  no  -> pause campaign (reason 'account')
         daily cap hit?    yes -> skip until tomorrow (campaign stays running)
         gap since last send elapsed?  no -> skip (random 3-7 min pacing)
         follow-up due but the lead replied? -> skip it (ladder stops)
         render -> refresh token if expired -> send via Gmail API
         429 -> pause + auto-resume after RATE_LIMIT_COOLDOWN_S
         401/403 -> mark account revoked + pause campaign
         other error -> retry later (attempts cap in the store)
         success -> mark sent + CRM event + queue the NEXT follow-up step
                    (not_before = now + after_days; replies cancel it)

One send per campaign per pass is deliberate: pacing lives BETWEEN passes,
so the thread can never machine-gun a queue even if the delay math is wrong.

Reply detection is BEST-EFFORT: a read failure never pauses a campaign or
revokes an account (only the send path does that), and accounts connected
before Phase E4 (no gmail.readonly in their granted scopes) are skipped
honestly with a log line instead of erroring on every check.

All network I/O goes through app.email_accounts.google (monkeypatched in
tests); the clock, RNG, and reply-check interval are injectable so every
branch is testable without sleeping.
"""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.campaigns.personalize import Ask, default_ask, generate_hook
from app.campaigns.store import CampaignStore
from app.campaigns.templates import context_for, render
from app.email_accounts import google
from app.email_accounts.store import EmailAccountStore
from app.lead_research.models import CRM_STATUSES
from app.lead_research.service import LeadResearchStore

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 20          # scheduler pass cadence
RATE_LIMIT_COOLDOWN_S = 3600   # a 429 backs off for an hour
MAX_ATTEMPTS = 3               # per-lead retries before 'failed'
TOKEN_EXPIRY_MARGIN_S = 60     # refresh this early, not mid-send
REPLY_CHECK_INTERVAL_S = 600   # inbox checked at most every 10 min per account
REPLY_OVERLAP_S = 600          # re-read 10 min of inbox so boundary mails aren't missed
REPLY_LOOKBACK_DAYS = 7        # first-ever check window when no last_check exists
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


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


def ensure_access_token(email_store: EmailAccountStore, *, account_id: int,
                        user_id: str, creds: dict[str, Any],
                        now: datetime) -> str:
    """A valid access token, refreshing it if expired. Empty string =
    refresh failed (caller decides — the send path pauses the campaign /
    refuses a test send, reply detection simply waits)."""
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
    email_store.update_tokens(
        account_id, user_id, access_token=new_access,
        token_expires_at=expires_at,
    )
    return new_access


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
        reply_interval_s: float = REPLY_CHECK_INTERVAL_S,
        ai_ask: Ask | None = None,
    ) -> None:
        self._store = store
        self._email_store = email_store
        self._lead_store = lead_store
        self._clock = clock
        self._rng = rng
        self._reply_interval_s = reply_interval_s
        # The AI callable for opening lines (E5). None = build the real
        # gateway lazily on first personalization; tests inject a fake.
        self._ai_ask = ai_ask
        # Per-account 429 cooldowns (E5): account_id -> send-again-not-before.
        # In-memory on purpose — a cooldown is transient, and a restart at
        # worst re-earns one 429 from Google.
        self._cooldowns: dict[int, datetime] = {}
        # Round-robin counter for tie-breaking equal accounts (E5).
        self._rr = 0
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
                 "resumed_account": 0, "sent": 0, "paused": 0,
                 "replied": 0, "skipped": 0}

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

        self._check_replies(now, stats)

        for c in self._store.running_campaigns():
            self._drain_one(c, now, stats)
        return stats

    # -- Reply detection (Phase E4) ---------------------------------------

    def _check_replies(self, now: datetime, stats: dict[str, int]) -> None:
        """For every account with sent-but-unreplied leads, read the recent
        inbox senders and match. Throttled per account; best-effort — a
        failure here never touches a campaign or the account's status."""
        for account_id, user_id in self._store.accounts_with_activity():
            last = parse_ts(self._store.get_reply_check(account_id))
            if last is not None and (now - last).total_seconds() < self._reply_interval_s:
                continue
            # Stamp BEFORE the network call — a failing read must not turn
            # into a Google-API hammer on the next pass.
            self._store.set_reply_check(account_id, _iso(now))

            sent = self._store.unreplied_sent(account_id)
            if not sent:
                continue
            creds = self._email_store.get_credentials(account_id, user_id)
            if creds is None:
                continue
            if READONLY_SCOPE not in (creds.get("scopes") or ""):
                # Connected before Phase E4: sending still works, replies
                # are simply not detected until the user reconnects.
                logger.info(
                    "reply detection skipped for %s: no gmail.readonly "
                    "(reconnect the account to enable it)", creds["email"])
                continue
            access_token = self._access_token(
                account_id=account_id, user_id=user_id, creds=creds, now=now)
            if not access_token:
                continue  # token trouble surfaces on the send path; replies wait

            if last is not None:
                after_unix = int(last.timestamp()) - REPLY_OVERLAP_S
            else:
                earliest = min(
                    (parse_ts(s["sent_at"]) for s in sent if parse_ts(s["sent_at"])),
                    default=now - timedelta(days=REPLY_LOOKBACK_DAYS))
                after_unix = int(earliest.timestamp()) - REPLY_OVERLAP_S

            try:
                senders = google.list_inbox_senders(
                    access_token, after_unix=after_unix)
            except Exception as exc:  # noqa: BLE001 — best-effort by design
                logger.warning(
                    "reply check for %s failed: %s (campaigns unaffected)",
                    creds["email"], exc)
                continue

            by_email = {s["email"]: s for s in sent}
            for msg in senders:
                addr = google.parse_from(msg.get("from", ""))
                target = by_email.get(addr)
                if target is None:
                    continue  # mail from someone we never emailed — not ours
                subject = (msg.get("subject") or "")[:300]
                self._store.mark_replied(
                    target["campaign_id"], addr,
                    received_at=_iso(now), subject=subject)
                stats["replied"] += 1
                logger.info("reply detected: %s answered campaign %d",
                            addr, target["campaign_id"])
                self._crm_event(
                    addr, user_id=target["user_id"], promote_to="replied",
                    note=f"lead replied (subject: {subject[:120]})")

    # -- One campaign, at most one send ------------------------------------

    def _pick_account(self, c: dict[str, Any],
                      now: datetime) -> tuple[int | None, str]:
        """Choose the sending account for this pass (E5 multi-account).

        Returns (account_id, "") on success, else (None, reason):
        'account'      — every account is disconnected/unhealthy -> pause
        'rate_limited' — every healthy account is in 429 cooldown -> pause
                         (with auto-resume at the earliest expiry)
        'capped'       — healthy accounts exist but all hit the daily cap ->
                         wait for tomorrow, campaign stays running

        Among the available accounts the one with the FEWEST sends today
        wins (load spreading), ties rotating so two fresh accounts
        alternate."""
        accounts = self._store.campaign_accounts(c["id"]) or [c["account_id"]]
        healthy: list[int] = []
        for a in accounts:
            creds = self._email_store.get_credentials(a, c["user_id"])
            if creds is None:
                continue
            status = next((s["status"] for s in
                           self._email_store.list_for_user(c["user_id"])
                           if s["id"] == a), "")
            if (status == "connected"
                    and (creds["access_token"] or creds["refresh_token"])):
                healthy.append(a)
        if not healthy:
            return None, "account"

        today = _iso(now)[:10]
        cooling = [a for a in healthy
                   if self._cooldowns.get(a, now) > now]
        avail = [a for a in healthy if a not in cooling
                 and self._store.sent_today_for_account(a, today)
                 < int(c["daily_limit"])]
        if not avail:
            if len(cooling) == len(healthy):
                return None, "rate_limited"
            return None, "capped"

        sent_today = {a: self._store.sent_today_for_account(a, today)
                      for a in avail}
        least = min(sent_today.values())
        tied = [a for a in avail if sent_today[a] == least]
        # Rotate through the tie so equal accounts alternate sends.
        self._rr += 1
        return tied[self._rr % len(tied)], ""

    def _earliest_cooldown(self, c: dict[str, Any], now: datetime) -> str:
        accounts = self._store.campaign_accounts(c["id"]) or [c["account_id"]]
        times = [self._cooldowns[a] for a in accounts
                 if a in self._cooldowns and self._cooldowns[a] > now]
        return _iso(min(times)) if times else _iso(now + timedelta(
            seconds=RATE_LIMIT_COOLDOWN_S))

    def _drain_one(self, c: dict[str, Any], now: datetime,
                   stats: dict[str, int]) -> None:
        account_id, reason = self._pick_account(c, now)
        if account_id is None:
            if reason == "account":
                # Every account gone (disconnected) -> honest pause,
                # auto-resume when any account reconnects.
                self._store.set_status(c["id"], status="paused",
                                       paused_reason="account")
                stats["paused"] += 1
                logger.warning("campaign %d paused: no healthy sending account",
                               c["id"])
            elif reason == "rate_limited":
                resume_at = self._earliest_cooldown(c, now)
                self._store.set_status(
                    c["id"], status="paused", paused_reason="rate_limited",
                    resume_at=resume_at)
                stats["paused"] += 1
                logger.warning(
                    "campaign %d: all accounts rate-limited -> paused, "
                    "auto-resume at %s", c["id"], resume_at)
            # 'capped' — nothing more today, campaign stays running.
            return

        send = self._store.next_pending(c["id"], _iso(now))
        if send is None:
            self._store.mark_completed_if_drained(c["id"])
            return

        # Random pacing, measured PER ACCOUNT (E5): Gmail's sending rhythm
        # is per account, so with N accounts each keeps its own 3-7 min gap
        # and the campaign's daily volume scales with N. A fresh draw each
        # pass — human-jitter by construction.
        last = parse_ts(self._store.last_sent_at_for_account(account_id))
        if last is not None:
            gap = self._rng(int(c["delay_min_s"]), int(c["delay_max_s"]))
            if (now - last).total_seconds() < gap:
                return

        # A follow-up whose lead answered in the window between queueing and
        # now: drop it (the reply path usually catches this first).
        if send["step"] > 0 and self._store.is_replied(c["id"], send["email"]):
            self._store.mark_skipped(send["id"], error="lead replied")
            stats["skipped"] += 1
            return

        # The lead's dossier is the ONLY source of template facts.
        dossier = self._lead_store.get(send["email"])
        if dossier is None:
            self._store.mark_failed(send["id"], error="lead dossier not found")
            self._store.mark_completed_if_drained(c["id"])
            return
        ctx = context_for(dossier)
        if send["step"] > 0:
            fu = self._store.followup(c["id"], send["step"])
            if fu is None:  # definition vanished — honest failure, not a guess
                self._store.mark_failed(send["id"],
                                        error=f"follow-up step {send['step']} not defined")
                self._store.mark_completed_if_drained(c["id"])
                return
            subject_tmpl, body_tmpl = fu["subject"], fu["body"]
        else:
            subject_tmpl, body_tmpl = c["subject"], c["body"]
        subject = render(subject_tmpl, ctx)
        body = render(body_tmpl, ctx)
        # The AI opening line (E5): first email only, from VERIFIED dossier
        # evidence only, cached per lead, best-effort by design.
        if send["step"] == 0 and c.get("ai_personalize"):
            body = self._with_hook(c, send, dossier, body)

        creds = self._email_store.get_credentials(account_id, c["user_id"])
        access_token = self._access_token(
            account_id=account_id, user_id=c["user_id"], creds=creds,
            now=now)
        if not access_token:
            self._email_store.mark_status(account_id, c["user_id"], "revoked")
            remaining, rreason = self._pick_account(c, now)
            if remaining is None and rreason == "account":
                self._store.set_status(c["id"], status="paused",
                                       paused_reason="account")
                stats["paused"] += 1
            logger.warning("campaign %d: token refresh failed for account %s",
                           c["id"], creds["email"])
            return

        try:
            google.send_gmail(
                access_token, to=send["email"], subject=subject, body=body,
                from_email=creds["email"],
            )
        except Exception as exc:  # noqa: BLE001 — mapped below by cause
            self._on_send_error(c, send, exc, stats, account_id=account_id,
                                now=now)
            return

        self._store.mark_sent(send["id"], subject=subject, sent_at=_iso(now),
                              account_id=account_id)
        stats["sent"] += 1
        logger.info("campaign %d sent to %s (step %d, account %d)",
                    c["id"], send["email"], send["step"], account_id)
        # CRM: the first outbound email moves the lead forward to
        # 'contacted'; follow-ups only append a timeline note (a reply or a
        # manually-set later stage is never walked backwards).
        if send["step"] == 0:
            self._crm_event(
                send["email"], user_id=c["user_id"], promote_to="contacted",
                note=f"email sent via campaign '{c['name']}': {subject}")
        else:
            self._crm_event(
                send["email"], user_id=c["user_id"],
                note=f"follow-up #{send['step']} sent via campaign "
                     f"'{c['name']}': {subject}")

        # Queue the next ladder rung for this lead (not_before = now +
        # after_days) — cancelled later if they reply before it fires.
        after_days = self._store.followup_after_days(
            c["id"], send["step"] + 1)
        if after_days is not None and not self._store.is_replied(
                c["id"], send["email"]):
            not_before = _iso(now + timedelta(days=after_days))
            self._store.queue_followup(
                c["id"], send["email"], step=send["step"] + 1,
                not_before=not_before)

        self._store.mark_completed_if_drained(c["id"])

    def _with_hook(self, c: dict[str, Any], send: dict[str, Any],
                   dossier: Any, body: str) -> str:
        """Prepend the lead's AI opening line to the rendered body. Cached
        per (campaign, lead); an AI failure sends the plain template (logged)
        and never blocks the campaign."""
        hook = self._store.get_hook(c["id"], send["email"])
        if hook is None:
            if self._ai_ask is None:
                self._ai_ask = default_ask()
            try:
                hook = generate_hook(self._ai_ask, dossier)
            except Exception as exc:  # noqa: BLE001 — best-effort by design
                logger.warning("AI opening line for %s failed: %s "
                               "(sending without one)", send["email"], exc)
                return body
            self._store.set_hook(c["id"], send["email"], hook)
        if not hook:
            return body  # generated, nothing honest to say
        return f"{hook}\n\n{body}"

    def _crm_event(self, email: str, *, user_id: str,
                   note: str, promote_to: str | None = None) -> None:
        """Append a CRM timeline note, and move the stage FORWARD to
        ``promote_to`` only when that is genuinely a step ahead (a lead
        already at 'meeting' who replies is not demoted to 'replied')."""
        if promote_to is None:
            self._lead_store.set_crm(email, note=note, user_id=user_id,
                                     username="")
            return
        current = (self._lead_store.get_crm(email) or {}).get("crm_status", "")
        if (current in CRM_STATUSES and promote_to in CRM_STATUSES
                and CRM_STATUSES.index(current) >= CRM_STATUSES.index(promote_to)):
            self._lead_store.set_crm(email, note=note, user_id=user_id,
                                     username="")
        else:
            self._lead_store.set_crm(email, status=promote_to, note=note,
                                     user_id=user_id, username="")

    def _access_token(self, *, account_id: int, user_id: str,
                      creds: dict[str, Any], now: datetime) -> str:
        """Delegates to the shared ``ensure_access_token`` (also used by the
        campaigns test-send endpoint — one refresh path, not two)."""
        return ensure_access_token(
            self._email_store, account_id=account_id, user_id=user_id,
            creds=creds, now=now)

    def _on_send_error(self, c: dict[str, Any], send: dict[str, Any],
                       exc: Exception, stats: dict[str, int], *,
                       account_id: int, now: datetime) -> None:
        """Map a Gmail failure to the honest campaign/account action (E5:
        account-scoped — a sick account steps aside, the campaign only
        pauses when NO account is left)."""
        resp = getattr(exc, "response", None)
        code = resp.status_code if resp is not None else None
        if code == 429:
            # Rate limited: cool THIS account down, self-healing. The
            # campaign pauses only when no other account is available.
            self._cooldowns[account_id] = now + timedelta(
                seconds=RATE_LIMIT_COOLDOWN_S)
            remaining, reason = self._pick_account(c, now)
            if remaining is None and reason == "rate_limited":
                resume_at = self._earliest_cooldown(c, now)
                self._store.set_status(
                    c["id"], status="paused", paused_reason="rate_limited",
                    resume_at=resume_at)
                stats["paused"] += 1
                logger.warning(
                    "campaign %d: all accounts rate-limited -> paused, "
                    "auto-resume at %s", c["id"], resume_at)
            else:
                logger.warning(
                    "campaign %d: account %d rate-limited -> cooling down, "
                    "other accounts continue", c["id"], account_id)
            return
        if code in (401, 403):
            # Grant revoked / permission gone: the account is unhealthy and
            # steps aside (marked revoked). The campaign pauses only when no
            # healthy account remains; it auto-resumes on reconnect.
            self._email_store.mark_status(account_id, c["user_id"], "revoked")
            remaining, reason = self._pick_account(c, now)
            if remaining is None and reason == "account":
                self._store.set_status(c["id"], status="paused",
                                       paused_reason="account")
                stats["paused"] += 1
            logger.warning("campaign %d: Gmail %s on account %d -> "
                           "account marked revoked", c["id"], code, account_id)
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
