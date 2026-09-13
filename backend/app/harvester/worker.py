"""The background harvester — the 24/7 stocker (P6, big-bang plan).

One daemon thread (the campaign-scheduler pattern: started by the app
lifespan, NEVER at import time so pytest never spawns a real harvest
loop). Every pass, under admission control so it never competes with
live user searches:

    PHONES lane   pick the neediest trade×state pair from the SODA
                  coverage map — ranked by user demand, then pool
                  deficit, then oldest harvest — and stock it with a
                  free license-board fetch. One pair every
                  HARVEST_PAIR_COOLDOWN_S at most; daily quota 5,000
                  stocked rows (the big-bang number).
    EMAILS lane   harvest-time AI: for the most-demanded trade×state
                  pair nobody has harvested recently, run the full
                  research pipeline (``run_full``) with ``user_id=""``
                  so the dossiers land SHARED — any user's search can
                  serve them instantly. Runs ONLY on logged demand: the
                  AI lane never spends without a signal. Daily quota
                  2,000 researched leads.
    RE-VERIFY     when both lanes come back empty, drain the staleness
                  queue (pairs whose stock has gone stale) — a fresh
                  harvest for one queued pair per idle pass.

Everything is honestly counted in ``harvester.db`` (demand, quotas, run
history) — the telemetry to answer "what is the stocker doing and why".

All lanes are injectable so every branch is testable without network,
AI, or sleeping.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from app.discovery.tradefold import trade_label
from app.harvester.store import HarvesterStore, US_STATE_NAMES
from app.leads.pipeline import ResearchQuery
from app.phones.soda import TRADE_COVERAGE, fetch_license_records
from app.phones.store import PhoneLeadsStore

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime | None:
    """Harvester-store timestamp -> aware datetime (naive treated as UTC)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class HarvesterWorker:
    """Owns the harvest loop. Constructed once by the app lifespan; tests
    construct their own with injected stores, lanes, and clock."""

    def __init__(
        self,
        store: HarvesterStore,
        phone_store: PhoneLeadsStore,
        lead_store: Any,
        pending_store: Any,
        *,
        fetch: Callable[[str, str, str, int],
                        tuple[Any, list[dict[str, Any]], dict[str, Any]]]
        | None = None,
        research: Callable[[ResearchQuery], dict[str, Any]] | None = None,
        active_jobs: Callable[[], int] | None = None,
        now: Callable[[], datetime] = _utcnow,
        enabled: bool = True,
        max_active: int = 4,
        interval_s: float = 300.0,
        phone_batch: int = 250,
        email_batch: int = 25,
        pair_cooldown_s: float = 21600.0,
        min_pool_floor: int = 100,
        staleness_days: int = 30,
        daily_phone_quota: int = 5000,
        daily_email_quota: int = 2000,
    ) -> None:
        self._store = store
        self._phone_store = phone_store
        self._lead_store = lead_store
        self._pending_store = pending_store
        # The phones lane's SODA fetch (source_id, slug, city, limit);
        # tests inject a fake so no pass ever touches the network.
        self._fetch = fetch or fetch_license_records
        # The emails lane's research pipeline (run_full wired to the real
        # stores in get_worker); tests inject a fake.
        self._research = research or self._default_research
        # Admission control: how many user pipelines hold ACTIVE slots right
        # now (JobManager.active_count in production). The harvester only
        # runs when there is spare budget — live searches always win.
        self._active_jobs = active_jobs or (lambda: 0)
        self._now = now
        self._enabled = enabled
        self._max_active = max_active
        self._interval_s = interval_s
        self._phone_batch = phone_batch
        self._email_batch = email_batch
        self._pair_cooldown_s = pair_cooldown_s
        self._min_pool_floor = min_pool_floor
        self._staleness_days = staleness_days
        self._daily_phone_quota = daily_phone_quota
        self._daily_email_quota = daily_email_quota
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="harvester", daemon=True,
        )
        self._thread.start()
        logger.info(
            "background harvester started (interval %ss, quotas %d phones / "
            "%d emails per day)", self._interval_s, self._daily_phone_quota,
            self._daily_email_quota,
        )

    def stop(self) -> None:
        self._stop.set()

    def _run_forever(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — one bad pass must not kill the loop
                logger.exception("harvester pass failed — retrying next pass")

    # -- one pass --------------------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        """One full pass; returns what happened (for logs + tests)."""
        stats: dict[str, Any] = {
            "skipped": "", "phones": None, "emails": None,
            "reverified": 0, "enqueued_stale": 0,
        }
        if not self._enabled:
            stats["skipped"] = "disabled"
            return stats
        # Admission control: live user searches fill the pipeline budget
        # first; the harvester only works with the spare capacity.
        if self._active_jobs() >= self._max_active:
            stats["skipped"] = "busy"
            return stats

        stats["phones"] = self._harvest_phones()
        stats["emails"] = self._harvest_emails()

        # Idle (nothing stocked by either lane) -> drain the staleness queue.
        if (not stats["phones"]["stocked"]) and (not stats["emails"]["stocked"]):
            stats["reverified"] = self._process_reverify()

        stats["enqueued_stale"] = self._enqueue_stale_pairs()
        if stats["phones"]["stocked"] or stats["emails"]["stocked"]:
            logger.info("harvester pass: phones=%s emails=%s",
                        stats["phones"], stats["emails"])
        return stats

    # -- phones lane (free SODA fetches into the pool) ----------------------------

    def _pick_phone_pair(self) -> tuple[str, str] | None:
        """The neediest harvestable trade×state pair, or None.

        Rank: user demand (weight) first, then pool deficit against the
        floor, then the longest-since-harvested. Pairs inside the cooldown
        window are skipped; a pair whose pool is at/over the floor AND has
        no demand is not worth a fetch at all.

        P7.5: trade-less searches record STATE-level demand rows (trade='').
        Their weight boosts EVERY covered pair of that state — a phone user
        who asks for "TX, 100 numbers" is pulling all trades at once, so
        the whole state's stocking rises to meet them.
        """
        now = self._now()
        rows = self._store.top_demand(200)
        demand = {
            (d["trade"], d["state"]): d["weight"] for d in rows
        }
        state_demand = {
            d["state"]: d["weight"]
            for d in rows if not d["trade"] and d["state"]
        }
        candidates: list[tuple[float, int, str, str, str]] = []
        for slug in sorted(TRADE_COVERAGE):
            for state in sorted(TRADE_COVERAGE[slug]):
                last = self._store.last_run_at("phones", slug, state)
                last_dt = _parse_ts(last)
                if (last_dt is not None
                        and (now - last_dt).total_seconds()
                        < self._pair_cooldown_s):
                    continue
                deficit = self._min_pool_floor - self._phone_store.unclaimed_count(
                    slug, state)
                weight = (demand.get((slug, state), 0.0)
                          + state_demand.get(state, 0.0))
                if deficit <= 0 and weight <= 0:
                    continue
                candidates.append((weight, deficit, last, slug, state))
        if not candidates:
            return None
        candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
        _, _, _, slug, state = candidates[0]
        return slug, state

    def _harvest_phone_pair(self, slug: str, state: str) -> dict[str, Any]:
        """Fetch + stock one trade×state pair (reused by the re-verify lane;
        the cooldown does NOT apply there — a stale pair is re-checked on
        purpose). Quota still applies: re-verify never bursts past the
        daily cap."""
        outcome = {"pair": (slug, state), "fetched": 0, "stocked": 0,
                   "skipped": ""}
        room = self._store.quota_room("phones", self._daily_phone_quota)
        if room <= 0:
            outcome["skipped"] = "quota_exhausted"
            return outcome
        source_id = TRADE_COVERAGE[slug][state]
        status, records, meta = self._fetch(
            source_id, slug, "", min(self._phone_batch, room),
        )
        if status.value != "success":
            # Honest failure + backoff: the run is recorded, so the pair
            # cools down instead of being retried every pass.
            self._store.record_run(
                "phones", slug, state, "source_error", 0,
                detail=str(meta.get("error", status.value))[:200],
            )
            outcome["skipped"] = "source_error"
            logger.warning(
                "harvester phones lane: %s/%s source %s unavailable: %s",
                slug, state, source_id, meta.get("error", ""),
            )
            return outcome
        counts = self._phone_store.add(records)
        self._store.record_stocked("phones", counts["inserted"])
        self._store.record_run(
            "phones", slug, state, "success", counts["inserted"],
            detail=(f"source={source_id} fetched={len(records)} "
                    f"dup={counts['duplicate']} "
                    f"dropped={counts['dropped_bad_phone']}"),
        )
        outcome["fetched"] = len(records)
        outcome["stocked"] = counts["inserted"]
        logger.info(
            "harvester phones lane: stocked %d row(s) for %s/%s "
            "(fetched %d, dup %d, dropped %d)",
            counts["inserted"], slug, state, len(records),
            counts["duplicate"], counts["dropped_bad_phone"],
        )
        return outcome

    def _harvest_phones(self) -> dict[str, Any]:
        pair = self._pick_phone_pair()
        if pair is None:
            return {"pair": None, "fetched": 0, "stocked": 0,
                    "skipped": "no_candidate"}
        return self._harvest_phone_pair(*pair)

    # -- emails lane (harvest-time AI, demand-gated) -------------------------------

    def _pick_email_pair(self) -> tuple[str, str] | None:
        """The most-demanded trade×state pair outside the cooldown, or None.

        The emails lane runs ONLY on demand: harvest-time AI spends real
        money, so it never runs for a pair no user has searched for.
        Demand rows without a parsable state are skipped — a location-less
        harvest cannot target anything.
        """
        now = self._now()
        for d in self._store.top_demand(200):
            trade, state = d["trade"], d["state"]
            if not trade or not state:
                continue
            last = self._store.last_run_at("emails", trade, state)
            last_dt = _parse_ts(last)
            if (last_dt is not None
                    and (now - last_dt).total_seconds() < self._pair_cooldown_s):
                continue
            return trade, state
        return None

    def _default_research(self, query: ResearchQuery) -> dict[str, Any]:
        """The real emails lane: run_full with SHARED dossiers (user_id="").
        Any user's later search serves them instantly from the pool."""
        from app.leads.pipeline import run_full
        return run_full(
            query,
            store=self._lead_store,
            pending_store=self._pending_store,
            user_id="",
        )

    def _harvest_emails(self) -> dict[str, Any]:
        outcome = {"pair": None, "target": 0, "stocked": 0, "skipped": ""}
        room = self._store.quota_room("emails", self._daily_email_quota)
        if room <= 0:
            outcome["skipped"] = "quota_exhausted"
            return outcome
        pair = self._pick_email_pair()
        if pair is None:
            outcome["skipped"] = "no_demand"
            return outcome
        trade, state = pair
        target = min(self._email_batch, room)
        # The demand row stores the canonical slug when one exists (the
        # hooks normalize); run_full gets the human-readable trade and the
        # full state name — the same vocabulary a user search would use.
        query = ResearchQuery(
            trade=trade_label(trade) or trade,
            location=US_STATE_NAMES.get(state, state),
            target_emails=target,
            search_name="harvester",
        )
        try:
            result = self._research(query)
        except Exception:  # noqa: BLE001 — one bad harvest must not kill the pass
            self._store.record_run("emails", trade, state, "error", 0)
            outcome["skipped"] = "error"
            logger.exception(
                "harvester emails lane: research failed for %s/%s",
                trade, state,
            )
            return outcome
        stocked = int(result.get("leads_found") or 0)
        self._store.record_stocked("emails", stocked)
        self._store.record_run(
            "emails", trade, state, "success", stocked,
            detail=(f"target={target} working={result.get('working_leads', 0)} "
                    f"shortfall={result.get('shortfall', 0)}"),
        )
        outcome["pair"] = pair
        outcome["target"] = target
        outcome["stocked"] = stocked
        logger.info(
            "harvester emails lane: researched %d lead(s) for %s/%s "
            "(target %d, working %s, shortfall %s)",
            stocked, trade, state, target,
            result.get("working_leads", 0), result.get("shortfall", 0),
        )
        return outcome

    # -- staleness re-verify --------------------------------------------------------

    def _process_reverify(self) -> int:
        """Drain one queued stale pair per idle pass (fresh harvest,
        cooldown waived, quota still respected)."""
        harvested = 0
        for item in self._store.take_reverify(1):
            slug, state = item["trade"], item["state"]
            if TRADE_COVERAGE.get(slug, {}).get(state):
                self._harvest_phone_pair(slug, state)
                harvested += 1
                logger.info(
                    "harvester re-verify: re-harvested %s/%s (%s)",
                    slug, state, item["reason"],
                )
        return harvested

    def _enqueue_stale_pairs(self) -> int:
        """Queue pairs whose last harvest is older than the staleness window
        AND whose pool has fallen below the floor. Idempotent per pending
        pair (one queue row each)."""
        now = self._now()
        enqueued = 0
        for slug in sorted(TRADE_COVERAGE):
            for state in sorted(TRADE_COVERAGE[slug]):
                last = _parse_ts(self._store.last_run_at("phones", slug, state))
                if last is None:
                    continue  # never harvested — a fresh pair, not a stale one
                if (now - last).days < self._staleness_days:
                    continue
                if self._phone_store.unclaimed_count(slug, state) >= \
                        self._min_pool_floor:
                    continue  # still well stocked — not worth a re-fetch
                self._store.enqueue_reverify(slug, state, "stale")
                enqueued += 1
        return enqueued


# App-level singleton wiring (lifespan start/stop; tests never touch this).
_worker: HarvesterWorker | None = None
_worker_lock = threading.Lock()


def get_worker() -> HarvesterWorker:
    """The process-wide harvester, wired to the real stores and settings."""
    global _worker
    with _worker_lock:
        if _worker is None:
            from app.core.config import settings
            from app.api.v1.leads import _manager
            from app.lead_research.service import (
                LeadResearchStore,
                PendingLeadsStore,
            )
            _worker = HarvesterWorker(
                HarvesterStore(),
                PhoneLeadsStore(),
                LeadResearchStore(),
                PendingLeadsStore(),
                active_jobs=lambda: _manager.active_count(),
                enabled=settings.HARVESTER_ENABLED,
                max_active=settings.MAX_ACTIVE_JOBS,
                interval_s=settings.HARVESTER_INTERVAL_S,
                phone_batch=settings.HARVEST_PHONE_BATCH,
                email_batch=settings.HARVEST_EMAIL_BATCH,
                pair_cooldown_s=settings.HARVEST_PAIR_COOLDOWN_S,
                min_pool_floor=settings.HARVEST_MIN_POOL_FLOOR,
                staleness_days=settings.HARVEST_STALENESS_DAYS,
                daily_phone_quota=settings.DAILY_PHONE_QUOTA,
                daily_email_quota=settings.DAILY_EMAIL_QUOTA,
            )
        return _worker
