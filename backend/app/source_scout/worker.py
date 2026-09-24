"""Daily, restart-safe source discovery using the existing scout pipeline."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.phones.store import PhoneLeadsStore
from app.source_scout import boards, bulk_sync, probation, proposals, verifier
from app.source_scout.store import ScoutStore, get_store

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScoutWorker:
    """Run one evidence-gated scout pass daily; retry failed passes hourly."""

    def __init__(
        self,
        store: ScoutStore,
        *,
        generate: Callable[..., dict[str, Any]] = proposals.generate_source_proposals,
        verify: Callable[..., dict[str, Any]] = verifier.verify_source,
        judge: Callable[..., dict[str, Any]] = probation.probation_pass,
        sync: Callable[..., dict[str, Any]] = bulk_sync.sync_promoted,
        sync_state_only: Callable[..., dict[str, Any]] = bulk_sync.sync_mn_state_only,
        sync_mn_trades: Callable[..., dict[str, Any]] = bulk_sync.sync_mn_verified_trades,
        sync_nyc_state_only: Callable[..., dict[str, Any]] = bulk_sync.sync_nyc_hic_state_only,
        phone_store: PhoneLeadsStore | None = None,
        ai_ask: Callable[[str], str] | None = None,
        now: Callable[[], datetime] = _utcnow,
        interval_s: float = 3600.0,
    ) -> None:
        self._store = store
        self._generate = generate
        self._verify = verify
        self._judge = judge
        self._sync = sync
        self._sync_state_only = sync_state_only
        self._sync_mn_trades = sync_mn_trades
        self._sync_nyc_state_only = sync_nyc_state_only
        self._phone_store = phone_store
        self._ai_ask = ai_ask
        self._now = now
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="source-scout", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — a source/AI failure cannot kill discovery
                logger.exception("source scout pass failed; retry is scheduled")
            if self._stop.wait(self._interval_s):
                break

    def run_once(self) -> dict[str, Any]:
        """One claimed pass through catalog, verification and probation."""
        now = self._now().astimezone(timezone.utc)
        stamp = now.isoformat(timespec="seconds")
        lease = (now + timedelta(hours=4)).isoformat(timespec="seconds")
        if not self._store.claim_scheduled_pass("source_discovery", stamp, lease):
            return {"skipped": "not_due"}

        success = False
        detail = ""
        try:
            seeded = boards.seed_registry(self._store)
            proposals.seed_known(self._store)
            synced = (
                self._sync(self._store, self._phone_store)
                if self._phone_store is not None else
                {"checked": [], "unchanged": [], "inserted": 0, "errors": {}}
            )
            state_only = (
                self._sync_state_only(self._store, self._phone_store)
                if self._phone_store is not None else
                {"checked": [], "unchanged": [], "inserted": 0, "errors": {}}
            )
            mn_trades = (
                self._sync_mn_trades(self._store, self._phone_store)
                if self._phone_store is not None else
                {"checked": [], "verified": 0, "ambiguous": 0, "errors": {}}
            )
            nyc_state_only = (
                self._sync_nyc_state_only(self._store, self._phone_store)
                if self._phone_store is not None else
                {"checked": [], "unchanged": [], "inserted": 0, "errors": {}}
            )
            ask = self._ai_ask or probation.default_ai_ask()
            generated = self._generate(self._store, ai_ask=ask)
            verified: list[str] = []
            for row in self._store.list_status("proposed"):
                result = self._verify(self._store, row["source_id"])
                if result["passed"]:
                    verified.append(row["source_id"])
            judged = self._judge(self._store, ai_ask=ask)
            detail = str(generated.get("reason", ""))[:500]
            ai_failed = "LLM call failed" in detail or any(
                "LLM call failed" in str(reason)
                for reason in judged.get("skipped", {}).values()
            )
            # A failing board is isolated by bulk_sync and tried again on
            # tomorrow's pass. It must not cause every healthy board to be
            # downloaded hourly together with an AI retry.
            success = not ai_failed
            outcome = {
                "seeded": seeded,
                "synced": synced,
                "state_only": state_only,
                "mn_trades": mn_trades,
                "nyc_state_only": nyc_state_only,
                "proposed": generated.get("proposed", []),
                "verified": verified,
                "promoted": judged.get("promoted", []),
                "reason": detail,
                "retry": not success,
            }
            logger.info("source scout pass: %s", outcome)
            return outcome
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:500]
            logger.exception("source scout pass failed: %s", detail)
            return {"error": detail, "retry": True}
        finally:
            # A successful pass is due again tomorrow. Failed AI/network
            # work is retried after an hour, including across app restarts.
            finished = self._now().astimezone(timezone.utc)
            next_due = finished + timedelta(
                days=1) if success else finished + timedelta(hours=1)
            self._store.finish_scheduled_pass(
                "source_discovery",
                now=finished.isoformat(timespec="seconds"),
                next_due_at=next_due.isoformat(timespec="seconds"),
                outcome="success" if success else "retry",
                detail=detail,
            )


_worker: ScoutWorker | None = None
_worker_lock = threading.Lock()


def get_worker() -> ScoutWorker:
    """Process-wide worker; construction itself never starts a thread."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = ScoutWorker(get_store(), phone_store=PhoneLeadsStore())
        return _worker
