"""One bounded worker for user-uploaded lead research."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from app.auth import dependencies as auth_deps
from app.lead_research.scoring import regate_verdict
from app.lead_research.service import LeadResearchService, LeadResearchStore
from app.leads.import_research_store import ImportResearchStore

logger = logging.getLogger(__name__)


class ImportResearchWorker:
    def __init__(self, store: ImportResearchStore,
                 lead_store: LeadResearchStore,
                 *, agent_factory: Callable[[], Any] | None = None) -> None:
        self._store = store
        self._leads = lead_store
        self._agent_factory = agent_factory or (lambda: LeadResearchService(store=lead_store).agent)
        self._agent: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        recovered = self._store.recover_interrupted()
        if recovered:
            logger.warning("resumed %d interrupted upload research rows", recovered)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="uploaded-lead-research", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("uploaded lead research worker pass failed")
                worked = False
            if self._stop.wait(1 if worked else 30):
                break

    def run_once(self) -> bool:
        """Process at most one address, preserving capacity for other workers."""
        for row in self._store.next_rows():
            user_id, email, job_id = row["user_id"], row["email"], row["job_id"]
            user = auth_deps._user_store().get_by_id(user_id)
            is_owner = bool(user and user.is_primary_owner)
            day = datetime.now(timezone.utc).date().isoformat()
            existing = self._leads.get(email)
            relevant_existing = existing is not None and regate_verdict(existing)[0] != "skip"
            already_owned = relevant_existing and self._leads.owned_by(email, user_id)
            prior_claim = self._store.claim_status(user_id, email, day)
            if already_owned and prior_claim != "reserved":
                if self._store.mark_working(row["id"], job_id):
                    self._store.finish_row(row["id"], job_id, "existing")
                    return True
                continue
            claim = self._store.reserve(user_id, email, job_id, day, is_owner=is_owner)
            if claim == "full":
                self._store.mark_waiting_quota(job_id)
                continue
            if not self._store.mark_working(row["id"], job_id):
                continue
            try:
                if self._store.is_cancelled(job_id):
                    self._store.finish_row(row["id"], job_id, "cancelled",
                                           user_id=user_id, email=email, claim_day=day)
                    return True
                if not relevant_existing:
                    if self._agent is None:
                        self._agent = self._agent_factory()
                    dossier = self._agent.research(email, email.rpartition("@")[2])
                else:
                    dossier = existing
                if (dossier.email or "").strip().lower() != email.lower():
                    raise ValueError("Research returned a different email address")
                if regate_verdict(dossier)[0] == "skip":
                    self._store.finish_row(row["id"], job_id, "irrelevant",
                                           "Research classified this lead as irrelevant",
                                           user_id=user_id, email=email, claim_day=day)
                    return True
                if self._store.is_cancelled(job_id):
                    self._store.finish_row(row["id"], job_id, "cancelled",
                                           user_id=user_id, email=email, claim_day=day)
                    return True
                if not relevant_existing:
                    self._leads.save(dossier, user_id=user_id)
                else:
                    self._leads.add_owner(email, user_id)
                self._store.finish_row(row["id"], job_id, "relevant",
                                       user_id=user_id, email=email, claim_day=day)
                logger.info("uploaded lead research kept %s for user %s", email, user_id)
                return True
            except Exception as exc:
                logger.exception("uploaded lead research failed for %s", email)
                # A save may have succeeded before a later accounting write
                # failed. Keep the reservation; a retry will reconcile it.
                if self._leads.owned_by(email, user_id):
                    self._store.retry_row(row["id"], str(exc))
                elif row["attempts"] + 1 < 3:
                    self._store.finish_row(row["id"], job_id, "pending", str(exc),
                                           user_id=user_id, email=email, claim_day=day)
                else:
                    self._store.finish_row(row["id"], job_id, "failed", str(exc),
                                           user_id=user_id, email=email, claim_day=day)
                return True
        return False


_worker: ImportResearchWorker | None = None
_lock = threading.Lock()


def get_worker() -> ImportResearchWorker:
    global _worker
    with _lock:
        if _worker is None:
            _worker = ImportResearchWorker(ImportResearchStore(), LeadResearchStore())
        return _worker
