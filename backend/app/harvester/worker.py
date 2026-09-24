"""The background harvester — the 24/7 stocker (P6, big-bang plan).

Two daemon lanes (started by the app lifespan, NEVER at import time)
run under admission control so live user searches take priority. The
phone lane keeps its own interval while email research is running:

    PHONES lane   pick the oldest unvisited trade×state pair from the SODA
                  coverage map, then use demand and pool deficit to break
                  ties, and stock it with a
                  free license-board fetch. One pair every
                  HARVEST_PAIR_COOLDOWN_S when configured; no daily
                  phone ceiling by default.
    EMAILS lane   harvest-time AI: for the most-demanded trade×state
                  pair nobody has harvested recently, run the full
                  research pipeline (``run_full``) with ``user_id=""``
                  so the dossiers land SHARED — any user's search can
                  serve them instantly. Runs ONLY on logged demand: the
                  AI lane never spends without a signal. Daily quota
                  2,000 researched leads.
    RE-VERIFY     when the phone lane comes back empty, drain the staleness
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
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app.discovery.tradefold import trade_label
from app.harvester.lane_schedule import (
    LaneDecision,
    LaneSpec,
    MODE_BOTH,
    MODE_EMAILS,
    MODE_PHONES,
    resolve,
)
from app.harvester.store import (
    HarvesterStore,
    US_STATE_NAMES,
    source_segment,
)
from app.leads.pipeline import ResearchQuery
from app.phones.soda import (
    fetch_license_records,
    fetchable_trade_coverage,
)
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
        email_budget_s: float = 2700.0,
        pair_cooldown_s: float = 0.0,
        min_pool_floor: int = 100,
        staleness_days: int = 30,
        daily_phone_quota: int = 0,
        daily_email_quota: int = 2000,
        lane_mode: str = "both",
        phone_slot_s: float = 1800.0,
        email_slot_s: float = 5400.0,
        phone_first: bool = True,
        lane_source: Callable[[], LaneDecision] | None = None,
    ) -> None:
        self._store = store
        self._phone_store = phone_store
        self._lead_store = lead_store
        self._pending_store = pending_store
        # The phones lane's SODA fetch (source_id, slug, city, limit,
        # offset=); tests inject a fake so no pass ever touches the network.
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
        self._email_budget_s = email_budget_s
        self._pair_cooldown_s = pair_cooldown_s
        self._min_pool_floor = min_pool_floor
        self._staleness_days = staleness_days
        self._daily_phone_quota = daily_phone_quota
        self._daily_email_quota = daily_email_quota
        # Lane control. Two sources feed ONE resolver (lane_schedule.resolve):
        #
        # * ``lane_source`` — production: the admin screen's live schedule
        #   (app/harvester/lane_schedule.py), re-read at the top of every
        #   pass, so an operator change lands on the next pass with no
        #   restart and no worker rebuild.
        # * the constructor args below — the boot default from .env, used by
        #   tests and by any deployment with no saved schedule (both lanes).
        #
        # Modes: "both" (both lanes), "phones" (emails blocked, zero AI),
        # "emails" (phones blocked), "auto" (alternate on a clock — the slot
        # lengths decide how long each lane runs; an ``auto`` schedule saved
        # with repeat="once" runs exactly one cycle then falls back to both).
        self._default_lane_mode = lane_mode
        self._default_phone_slot_s = phone_slot_s
        self._default_email_slot_s = email_slot_s
        self._default_phone_first = phone_first
        self._lane_source = lane_source or self._fallback_lane_source
        #: The lane the LAST pass actually ran — for the phase-transition log.
        self._was_in_phone_slot: bool | None = None
        self._one_time_logged = False
        # Emails-lane wall-clock deadline for the CURRENT pass (a
        # monotonic timestamp, or None when no pass is running). Set by
        # _harvest_emails, read by _budget_expired through run_full's
        # cancel seam — a plain bool flag could not express "the pass is
        # over, stop", only "someone asked to stop".
        self._email_deadline: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._email_thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="harvester-phones", daemon=True,
        )
        self._email_thread = threading.Thread(
            target=self._run_emails_forever, name="harvester-emails",
            daemon=True,
        )
        self._thread.start()
        self._email_thread.start()
        logger.info(
            "background harvester started (interval %ss, phone quota %s / "
            "email quota %d per day)", self._interval_s,
            self._daily_phone_quota or "unlimited",
            self._daily_email_quota,
        )

    def stop(self) -> None:
        self._stop.set()

    def _run_forever(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                if not self._enabled or self._active_jobs() >= self._max_active:
                    continue
                decision = self._current_lane()
                if decision.mode == MODE_EMAILS:
                    continue
                phones = self._harvest_phones()
                if not phones["stocked"]:
                    self._process_reverify()
                self._enqueue_stale_pairs()
                self._log_phase_state(decision.in_phone_slot)
            except Exception:  # noqa: BLE001 — one bad pass must not kill the loop
                logger.exception("harvester phone pass failed — retrying next pass")

    def _run_emails_forever(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                if not self._enabled or self._active_jobs() >= self._max_active:
                    continue
                if self._current_lane().mode != MODE_PHONES:
                    self._harvest_emails()
            except Exception:  # noqa: BLE001 — one failed AI pass cannot stop phones
                logger.exception("harvester email pass failed — retrying next pass")

    def _current_lane(self) -> LaneDecision:
        try:
            return self._lane_source()
        except Exception:  # noqa: BLE001 — a broken schedule must not stop stocking
            logger.exception("harvester lane schedule unreadable — running both lanes")
            return LaneDecision(mode=MODE_BOTH, spec=LaneSpec())

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

        # Lane control — resolved fresh EVERY pass, so an admin-screen change
        # lands on the next pass without a restart. A broken schedule source
        # degrades to "both" (never a stopped harvester) and says so.
        decision = self._current_lane()

        if decision.mode == MODE_BOTH:
            stats["phones"] = self._harvest_phones()
            stats["emails"] = self._harvest_emails()
        elif decision.mode == MODE_PHONES:
            # Emails blocked: this is the zero-AI pass by construction, since
            # the phones lane is pure SODA and never calls the model.
            stats["emails"] = {"pair": None, "target": 0, "stocked": 0,
                               "skipped": "lane_schedule_phones"}
            stats["phones"] = self._harvest_phones()
        else:  # MODE_EMAILS
            stats["phones"] = {"pair": None, "stocked": 0,
                               "skipped": "lane_schedule_emails"}
            stats["emails"] = self._harvest_emails()

        # Log phase transitions (visible in backend.log as a human cue).
        try:
            self._log_phase_state(decision.in_phone_slot)
            if decision.one_time_done and not self._one_time_logged:
                self._one_time_logged = True
                logger.info(
                    "harvester lane schedule: one-time cycle finished "
                    "(%g min) — back to both lanes",
                    decision.cycle_s / 60.0,
                )
            elif not decision.one_time_done:
                self._one_time_logged = False  # a fresh save re-arms the cue
        except Exception:  # noqa: BLE001 — a log failure must never crash the loop
            logger.exception("phase-state log failed")

        # Idle (nothing stocked by either lane) -> drain the staleness queue.
        if (not stats["phones"]["stocked"]) and (not stats["emails"]["stocked"]):
            stats["reverified"] = self._process_reverify()

        stats["enqueued_stale"] = self._enqueue_stale_pairs()
        if stats["phones"]["stocked"] or stats["emails"]["stocked"]:
            logger.info("harvester pass: phones=%s emails=%s",
                        stats["phones"], stats["emails"])
        return stats

    # -- lane control helpers --------------------------------------------------

    def _fallback_lane_source(self) -> LaneDecision:
        """The boot-default schedule (constructor args / .env).

        Used when no ``lane_source`` was injected: tests, and any deployment
        with no saved runtime schedule. Anchored at the current midnight UTC
        — the original phase_lock semantics, where slot boundaries are the
        same on every server sharing the clock. A saved admin schedule
        anchors at its own save time instead (lane_schedule.resolve).
        """
        now = self._now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spec = LaneSpec(
            mode=self._default_lane_mode,
            phone_min=self._default_phone_slot_s / 60.0,
            email_min=self._default_email_slot_s / 60.0,
            phone_first=self._default_phone_first,
            started_at=midnight.isoformat(timespec="seconds"),
        )
        return resolve(spec, now)

    def _log_phase_state(self, in_phone_slot: bool | None) -> None:
        """Emit one INFO per phase transition so the operator can see the
        cycle turn in backend.log without hunting for lane-specific lines.
        Called once per pass; ``None`` means no cycle is being tracked
        (pinned mode) and also clears the memory, so switching out of
        ``auto`` and back never reports a transition that did not happen."""
        if in_phone_slot is None:
            self._was_in_phone_slot = None
            return
        was_phone = self._was_in_phone_slot
        self._was_in_phone_slot = in_phone_slot
        if was_phone is not None and was_phone != in_phone_slot:
            lane = "phones → emails" if was_phone else "emails → phones"
            logger.info("harvester phase: %s", lane)

    # -- phones lane (free SODA fetches into the pool) ----------------------------

    def _pick_phone_pair(self) -> tuple[str, str] | None:
        """The longest-waiting harvestable trade×state pair, or None.

        A national sweep must visit pairs even after their pool reaches the
        old floor. Untouched pairs go first; demand and deficit break age
        ties. Explicit cooldowns still apply when configured.

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
        candidates: list[tuple[str, float, int, str, str]] = []
        coverage = fetchable_trade_coverage()
        for slug in sorted(coverage):
            for state in sorted(coverage[slug]):
                last = self._store.last_run_at("phones", slug, state)
                last_dt = _parse_ts(last)
                if (last_dt is not None
                        and (now - last_dt).total_seconds()
                        < self._pair_cooldown_s):
                    continue
                # P10 yield learning: a (source, pair) whose successful
                # fetches repeatedly stocked nothing new is asleep until
                # the drop re-arms — spend the pass elsewhere.
                if self._store.source_should_skip(
                        coverage[slug][state],
                        source_segment(slug, state),
                        rearm_days=self._staleness_days):
                    continue
                deficit = self._min_pool_floor - self._phone_store.unclaimed_count(
                    slug, state)
                weight = (demand.get((slug, state), 0.0)
                          + state_demand.get(state, 0.0))
                candidates.append((last, weight, deficit, slug, state))
        if not candidates:
            return None
        candidates.sort(key=lambda c: (c[0], -c[1], -c[2], c[3], c[4]))
        _, _, _, slug, state = candidates[0]
        return slug, state

    def _harvest_phone_pair(self, slug: str, state: str) -> dict[str, Any]:
        """Fetch + stock one trade×state pair (also used by re-verify).

        Re-verify bypasses pair cooldown; an explicit positive phone quota
        still limits a deployment that chooses to configure one.
        """
        outcome = {"pair": (slug, state), "fetched": 0, "stocked": 0,
                   "skipped": ""}
        room = (self._store.quota_room("phones", self._daily_phone_quota)
                if self._daily_phone_quota > 0 else self._phone_batch)
        if room <= 0:
            outcome["skipped"] = "quota_exhausted"
            return outcome
        source_id = fetchable_trade_coverage().get(slug, {}).get(state)
        if not source_id:
            # Honest skip: the source retired between selection and harvest
            # (scout circuit breaker) — the pair cools down via record_run.
            self._store.record_run(
                "phones", slug, state, "no_coverage", 0,
                detail="source retired before harvest",
            )
            outcome["skipped"] = "no_coverage"
            logger.info(
                "harvester phones lane: %s/%s lost coverage before harvest",
                slug, state,
            )
            return outcome
        if self._store.source_should_skip(
                source_id, source_segment(slug, state),
                rearm_days=self._staleness_days):
            # P10: this (source, pair) is yield-asleep — proven zero NEW
            # rows over enough successful fetches, drop still fresh. An
            # honest skip + cooldown, never a spend. (The reverify lane is
            # the explorer: once the re-arm window passes, should_skip
            # opens and one trial re-arms or re-earns the pair.)
            self._store.record_run(
                "phones", slug, state, "source_yield_zero", 0,
                detail="source proven zero-yield; sleeping until re-arm",
                source=source_id,
            )
            outcome["skipped"] = "source_yield_zero"
            logger.info(
                "harvester phones lane: %s/%s yield-asleep (source %s) — "
                "skipped", slug, state, source_id,
            )
            return outcome
        # The cursor is what makes repeated runs worth anything: without it
        # every run asked the source for the same first page and reported
        # 100% duplicates (measured live 2026-09-23 — WA gc has 55,184
        # matching rows and only the first 250 had ever been read).
        offset = self._store.cursor_get(source_id, slug, state)
        limit = min(self._phone_batch, room)
        status, records, meta = self._fetch(
            source_id, slug, "", limit, offset=offset,
        )
        if status.value != "success":
            # Honest failure + backoff: the run is recorded, so the pair
            # cools down instead of being retried every pass. NOT a yield
            # trial — hard failures are the scout circuit breaker's domain.
            # The cursor deliberately does NOT move: the window was never
            # read, so the next attempt must ask for the same one.
            self._store.record_run(
                "phones", slug, state, "source_error", 0,
                detail=str(meta.get("error", status.value))[:200],
                source=source_id,
            )
            outcome["skipped"] = "source_error"
            logger.warning(
                "harvester phones lane: %s/%s source %s unavailable: %s",
                slug, state, source_id, meta.get("error", ""),
            )
            return outcome
        # A page SHORTER than the one we asked for is the only honest proof
        # that the matching set ended here, so the cursor wraps to 0 and the
        # next run opens a fresh pass. Boards issue new licences weekly — a
        # frozen cursor would never see them.
        fetched_rows = int(meta.get("rows_fetched", len(records)))
        swept = fetched_rows < limit
        # P10: a successful fetch is one yield trial for (source, pair);
        # the working credit lands only if it stocked something NEW.
        seg = source_segment(slug, state)
        counts = self._phone_store.add(records)
        self._store.record_source_dispatch(source_id, seg)
        if counts["inserted"]:
            self._store.record_source_working(source_id, seg)
        self._store.record_stocked("phones", counts["inserted"])
        self._store.record_run(
            "phones", slug, state, "success", counts["inserted"],
            detail=(f"source={source_id} offset={offset} "
                    f"fetched={len(records)} "
                    f"dup={counts['duplicate']} "
                    f"dropped={counts['dropped_bad_phone']}"),
            source=source_id,
        )
        # The page is acknowledged only after it has reached the pool.
        # On a failed write, retry the same window; duplicates are benign.
        self._store.cursor_advance(
            source_id, slug, state,
            next_offset=0 if swept else offset + fetched_rows,
            swept=swept,
        )
        if swept and offset > 0:
            logger.info(
                "harvester phones lane: %s/%s sweep complete at offset %d "
                "(%d rows) — cursor wrapped to 0",
                slug, state, offset, fetched_rows,
            )
        outcome["fetched"] = len(records)
        outcome["stocked"] = counts["inserted"]
        outcome["offset"] = offset
        logger.info(
            "harvester phones lane: stocked %d row(s) for %s/%s "
            "(offset %d, fetched %d, dup %d, dropped %d)",
            counts["inserted"], slug, state, offset, len(records),
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

    def _budget_expired(self) -> bool:
        """run_full's cancel seam: True once the current emails pass has
        outrun its wall-clock budget (no pass running = not cancelled —
        run_full only ever calls this while one is)."""
        return (
            self._email_deadline is not None
            and time.monotonic() >= self._email_deadline
        )

    def _default_research(self, query: ResearchQuery) -> dict[str, Any]:
        """The real emails lane: run_full with SHARED dossiers (user_id="").
        Any user's later search serves them instantly from the pool.

        The pass carries a WALL-CLOCK BUDGET (``email_budget_s``, soft
        cancel seam): a discovery loop with no budget ground on for
        2.5 hours on the 2026-09-14 test server, blocking the harvester
        thread and stacking discovery data in RAM. The budget is checked
        between discovery passes and between researched leads, so
        whatever is researched by the deadline is banked (a shared
        dossier is a shared dossier); the pair's cooldown then splits
        the remaining work across later passes — the demand-gated lane
        never loses a pair, it just never grinds.
        """
        from app.leads.pipeline import run_full
        # P5-Lite DOA gate: heuristically-dead addresses (disposable /
        # authoritative no-MX / real bounce) never reach AI research.
        # Lazy so a broken bounce store degrades to heuristic-only, never
        # blocks the lane.
        try:
            from app.email.heuristic_verifier import get_email_classifier

            classifier = get_email_classifier()
        except Exception:  # noqa: BLE001 — the gate is best-effort
            classifier = None
        return run_full(
            query,
            store=self._lead_store,
            pending_store=self._pending_store,
            user_id="",
            cancel=self._budget_expired,
            email_classifier=classifier,
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
            self._email_deadline = (
                time.monotonic() + self._email_budget_s
                if self._email_budget_s > 0 else None
            )
            result = self._research(query)
        except Exception:  # noqa: BLE001 — one bad harvest must not kill the pass
            self._store.record_run("emails", trade, state, "error", 0)
            outcome["skipped"] = "error"
            logger.exception(
                "harvester emails lane: research failed for %s/%s",
                trade, state,
            )
            return outcome
        finally:
            self._email_deadline = None
        stocked = int(result.get("leads_found") or 0)
        self._store.record_stocked("emails", stocked)
        budget_note = (
            " budget_hit=yes (pass split — remainder on a later pass)"
            if result.get("shortfall_reason") == "harvest_budget_expired"
            else ""
        )
        self._store.record_run(
            "emails", trade, state, "success", stocked,
            detail=(f"target={target} working={result.get('working_leads', 0)} "
                    f"shortfall={result.get('shortfall', 0)}{budget_note}"),
        )
        outcome["pair"] = pair
        outcome["target"] = target
        outcome["stocked"] = stocked
        logger.info(
            "harvester emails lane: researched %d lead(s) for %s/%s "
            "(target %d, working %s, shortfall %s%s)",
            stocked, trade, state, target,
            result.get("working_leads", 0), result.get("shortfall", 0),
            budget_note,
        )
        return outcome

    # -- staleness re-verify --------------------------------------------------------

    def _process_reverify(self) -> int:
        """Drain one queued stale pair per idle pass (fresh harvest,
        cooldown waived, quota still respected). A yield-asleep source
        skips honestly — the queue item is still consumed (it will requeue
        on the next staleness pass if still eligible)."""
        harvested = 0
        for item in self._store.take_reverify(1):
            slug, state = item["trade"], item["state"]
            if fetchable_trade_coverage().get(slug, {}).get(state):
                out = self._harvest_phone_pair(slug, state)
                if out["skipped"]:
                    logger.info(
                        "harvester re-verify: %s/%s skipped (%s) — %s",
                        slug, state, out["skipped"], item["reason"],
                    )
                    continue
                harvested += 1
                logger.info(
                    "harvester re-verify: re-harvested %s/%s (%s)",
                    slug, state, item["reason"],
                )
        return harvested

    def _enqueue_stale_pairs(self) -> int:
        """Queue pairs whose last harvest is older than the staleness window
        AND whose pool has fallen below the floor. Idempotent per pending
        pair (one queue row each). Yield-asleep pairs are not queued — a
        re-fetch of a proven-zero source is a guaranteed no-op."""
        now = self._now()
        enqueued = 0
        coverage = fetchable_trade_coverage()
        for slug in sorted(coverage):
            for state in sorted(coverage[slug]):
                last = _parse_ts(self._store.last_run_at("phones", slug, state))
                if last is None:
                    continue  # never harvested — a fresh pair, not a stale one
                if (now - last).days < self._staleness_days:
                    continue
                if self._phone_store.unclaimed_count(slug, state) >= \
                        self._min_pool_floor:
                    continue  # still well stocked — not worth a re-fetch
                if self._store.source_should_skip(
                        coverage[slug][state],
                        source_segment(slug, state),
                        rearm_days=self._staleness_days):
                    continue  # yield-asleep — re-arming happens at pick time
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
            from app.harvester.lane_schedule import get_lane_store
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
                email_budget_s=settings.HARVEST_EMAIL_BUDGET_S,
                pair_cooldown_s=settings.HARVEST_PAIR_COOLDOWN_S,
                min_pool_floor=settings.HARVEST_MIN_POOL_FLOOR,
                staleness_days=settings.HARVEST_STALENESS_DAYS,
                daily_phone_quota=settings.DAILY_PHONE_QUOTA,
                daily_email_quota=settings.DAILY_EMAIL_QUOTA,
                lane_mode=settings.HARVEST_LANE_MODE,
                phone_slot_s=settings.HARVEST_PHONE_SLOT_S,
                email_slot_s=settings.HARVEST_EMAIL_SLOT_S,
                phone_first=settings.HARVEST_PHONE_FIRST,
                # The admin screen's LIVE schedule — read once per pass, so a
                # change there needs no restart and no worker rebuild. Falls
                # back to the .env defaults above when nothing is saved.
                lane_source=lambda: get_lane_store().resolve(_utcnow()),
            )
        return _worker
