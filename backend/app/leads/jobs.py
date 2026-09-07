"""Leads API — background job runner.

:class:`JobStore`  persists jobs to SQLite (``jobs`` table).
:class:`JobManager` submits a :class:`ResearchQuery`, runs it on a daemon
worker thread, and records live progress events + per-lead results as they
finish. A graceful ``cancel()`` flag is checked between discovery passes and
between leads.

The pipeline is synchronous/blocking (real search + AI calls), so a dedicated
worker thread is the right primitive — FastAPI's request thread returns
immediately with a ``job_id`` and the frontend polls ``GET /jobs/{id}`` for
live progress (poll-based telemetry, CLAUDE.md §6 honest logging).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.leads.models import Job, JobEvent, JobState
from app.leads.pipeline import ResearchQuery, run_full

logger = logging.getLogger(__name__)


def _now() -> str:
    """ISO-8601 UTC timestamp (naive, no zone suffix for clean JSON)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _elapsed_now(created_at: str) -> float:
    """Seconds between a UTC ``created_at`` (naive, no zone suffix) and now.

    Used by :meth:`JobManager.recover_orphans` so an orphaned job records its
    honest lifetime even though no worker thread is alive to write it.
    """
    try:
        start = datetime.fromisoformat(created_at)
    except (ValueError, TypeError):
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - start).total_seconds())


class JobStore:
    """SQLite persistence for jobs."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "output", "lead_research.db"
            )
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                query_json TEXT NOT NULL,
                state TEXT NOT NULL,
                events_json TEXT NOT NULL,
                results_json TEXT NOT NULL,
                pass_log_json TEXT NOT NULL,
                error TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                elapsed_s REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def save(self, job: Job) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            INSERT INTO jobs (id, query_json, state, events_json, results_json,
                              pass_log_json, error, created_at, updated_at, elapsed_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state = excluded.state,
                events_json = excluded.events_json,
                results_json = excluded.results_json,
                pass_log_json = excluded.pass_log_json,
                error = excluded.error,
                updated_at = excluded.updated_at,
                elapsed_s = excluded.elapsed_s
            """,
            (
                job.id,
                json.dumps(job.query),
                job.state.value,
                json.dumps([e.to_dict() for e in job.events]),
                json.dumps(job.results),
                json.dumps(job.pass_log),
                job.error,
                job.created_at,
                job.updated_at,
                job.elapsed_s,
            ),
        )
        conn.commit()
        conn.close()

    def get(self, job_id: str) -> Job | None:
        conn = sqlite3.connect(self._db_path)
        cur = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        )
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if row else []
        conn.close()
        if row is None:
            return None
        d = dict(zip(cols, row))
        return Job(
            id=d["id"],
            query=json.loads(d["query_json"]),
            state=JobState(d["state"]),
            events=[JobEvent.from_dict(e) for e in json.loads(d["events_json"])],
            results=json.loads(d["results_json"]),
            pass_log=json.loads(d["pass_log_json"]),
            error=d["error"],
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            elapsed_s=d["elapsed_s"],
        )

    def list_all(self) -> list[Job]:
        conn = sqlite3.connect(self._db_path)
        cur = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC"
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if rows else []
        conn.close()
        if not rows:
            return []
        return [
            Job(
                id=dict(zip(cols, r))["id"],
                query=json.loads(dict(zip(cols, r))["query_json"]),
                state=JobState(dict(zip(cols, r))["state"]),
                events=[JobEvent.from_dict(e) for e in json.loads(dict(zip(cols, r))["events_json"])],
                results=json.loads(dict(zip(cols, r))["results_json"]),
                pass_log=json.loads(dict(zip(cols, r))["pass_log_json"]),
                error=dict(zip(cols, r))["error"],
                created_at=dict(zip(cols, r))["created_at"],
                updated_at=dict(zip(cols, r))["updated_at"],
                elapsed_s=dict(zip(cols, r))["elapsed_s"],
            )
            for r in rows
        ]


class JobManager:
    """Submit and track background lead-research jobs."""

    def __init__(self, store: JobStore | None = None, db_path: str | None = None) -> None:
        self._store = store or JobStore(db_path=db_path)
        self._lock = threading.RLock()
        self._cancel_flags: dict[str, bool] = {}
        # One threading.Event per job: set => running, cleared => paused. The
        # worker blocks in ``_wait_if_paused`` while its event is cleared.
        self._pause_events: dict[str, threading.Event] = {}
        # Live worker threads — for on-the-fly dead-worker detection when the
        # process is alive but a worker died (root cause of "running stuck").
        self._workers: dict[str, threading.Thread] = {}

    def recover_orphans(self) -> int:
        """Mark jobs stuck in a live state as failed (root-cause fix).

        A job left ``queued``/``running``/``paused`` when a JobManager boots
        CANNOT belong to this process — its worker thread died with the
        previous process (server restart, TaskStop, crashed uvicorn). Before
        this existed such rows showed "Running" forever (CLAUDE.md §12: silent
        phantom state). We fail them so History/Dashboard tell the truth and
        the run's query can be re-submitted (pipeline cross-run dedup skips
        already-researched dossiers).

        Called ONCE from the server's FastAPI lifespan startup — deliberately
        NOT from ``__init__``, so merely importing the module (pytest, other
        scripts) can never sweep a genuinely-running job in another process.

        Returns how many jobs were failed.
        """
        recovered = 0
        for job in self._store.list_all():
            if job.state not in (JobState.queued, JobState.running, JobState.paused):
                continue
            prev_state = job.state.value
            job.state = JobState.failed
            job.error = "interrupted by server restart; re-run to continue"
            job.elapsed_s = _elapsed_now(job.created_at)
            job.updated_at = _now()
            self._store.save(job)
            recovered += 1
            logger.warning(
                "Job %s was %s at boot (%s) — marked failed (orphan from a "
                "previous process); re-run the same query to continue",
                job.id, prev_state, job.created_at,
            )
        if recovered:
            logger.warning("recover_orphans: %d in-flight job(s) failed at boot", recovered)
        return recovered

    # -- lifecycle ----

    def submit(self, query: ResearchQuery) -> Job:
        """Validate, persist a queued job, and start its worker thread."""
        query.validate()
        job = Job(
            id=uuid.uuid4().hex[:12],
            query=query.to_dict(),
            state=JobState.queued,
            created_at=_now(),
            updated_at=_now(),
        )
        self._store.save(job)
        self._cancel_flags[job.id] = False
        ev = threading.Event()
        ev.set()  # running by default
        self._pause_events[job.id] = ev
        t = threading.Thread(target=self._run, args=(job.id,), daemon=True)
        t.start()
        self._workers[job.id] = t
        logger.info("Job %s submitted: %s", job.id, query.describe())
        return job

    def cancel(self, job_id: str) -> bool:
        """Request a graceful stop. Returns False if the job is unknown."""
        with self._lock:
            job = self._store.get(job_id)
            if job is None or job.state in (JobState.completed, JobState.failed, JobState.cancelled):
                return False
            self._cancel_flags[job_id] = True
            return True

    def pause(self, job_id: str) -> bool:
        """Pause a running job. The worker blocks until resumed or cancelled.

        Returns False if the job is unknown or not running. Leads already
        researched remain persisted and visible.
        """
        with self._lock:
            job = self._store.get(job_id)
            if job is None or job.state != JobState.running:
                return False
            self._pause_events[job_id].clear()
            job.state = JobState.paused
            job.updated_at = _now()
            self._store.save(job)
            return True

    def resume(self, job_id: str) -> bool:
        """Resume a paused job — or CONTINUE a job interrupted by a restart.

        A job whose worker died when the previous server process went away is
        marked ``failed`` by :meth:`recover_orphans` with NO live thread. This
        is the "continue the interrupted search from where it left off" path:

          - ``paused``  -> the existing worker is un-paused (event set);
          - ``failed``  -> a FRESH worker is launched on the SAME job id,
            KEEPING the events/results/pass_log already accumulated. The
            pipeline's own durable state provides the continuity — saved
            dossiers are skipped (cross-run dedup), dead domains are never
            re-served (dead pool), pending cache serves where it left off.

        Returns False when the job is unknown or not resumable (queued/running/
        completed/cancelled).
        """
        with self._lock:
            job = self._store.get(job_id)
            if job is None:
                return False
            if job.state is JobState.paused:
                self._pause_events[job_id].set()
                job.state = JobState.running
                job.updated_at = _now()
                self._store.save(job)
                return True
            if job.state is JobState.failed:
                # Restart-interruption continuity. Reset the accumulated
                # elapsed (the orphan value includes the down-window) and the
                # worker's cancel/pause handles, then re-launch on the SAME id
                # so History shows ONE continuous run; accumulated results and
                # the progress log carry over untouched.
                job.elapsed_s = 0.0
                job.error = ""
                job.state = JobState.running
                job.updated_at = _now()
                self._store.save(job)
                self._cancel_flags[job_id] = False
                ev = threading.Event()
                ev.set()
                self._pause_events[job_id] = ev
                t = threading.Thread(target=self._run, args=(job_id,), daemon=True)
                t.start()
                self._workers[job_id] = t
                logger.info("Job %s continued after interruption (fresh worker)", job_id)
                return True
            return False

    # -- dead-worker sweep (on-the-fly orphan detection) ----

    def sweep_dead_workers(self) -> int:
        """Mark live-state jobs whose worker thread has died as failed.

        Covers the case where the process is still running but a worker
        thread exited (exception, killed externally, TaskStop) — the DB
        still says "running" and ``recover_orphans`` won't run until the
        next process boot. Called lazily from ``get()``/``list_jobs()`` so
        every read returns honest state.

        Only sweeps jobs this process OWNS (tracked in ``_workers``). Cross-
        process jobs are swept by ``recover_orphans`` at boot time.
        """
        count = 0
        with self._lock:
            for jid in list(self._workers):
                thread = self._workers.get(jid)
                if thread is None or thread.is_alive():
                    continue
                job = self._store.get(jid)
                if job is None or job.state not in (JobState.queued, JobState.running, JobState.paused):
                    continue
                # Worker is dead but DB says live → mark failed now.
                self._workers.pop(jid, None)
                job.state = JobState.failed
                job.error = (
                    "worker thread interrupted (server busy/restart); "
                    "click Continue to resume"
                )
                job.elapsed_s += _elapsed_now(job.updated_at)
                job.updated_at = _now()
                self._store.save(job)
                count += 1
                logger.warning(
                    "sweep: job %s worker dead (state=%s) → marked failed",
                    jid, job.state.value,
                )
        return count

    def get(self, job_id: str) -> Job | None:
        self.sweep_dead_workers()
        return self._store.get(job_id)

    def list_jobs(self) -> list[Job]:
        self.sweep_dead_workers()
        return self._store.list_all()

    # -- worker ----

    def _run(self, job_id: str) -> None:
        query = ResearchQuery.from_dict(self._store.get(job_id).query)
        t0 = datetime.now(timezone.utc)

        with self._lock:
            job = self._store.get(job_id)
            job.state = JobState.running
            job.updated_at = _now()
            self._store.save(job)

        def emit(
            phase: str, step: int, total: int, message: str,
            email: str = "", data: dict | None = None,
        ) -> None:
            self._append_event(job_id, JobEvent(
                phase=phase, step=step, total=total, message=message,
                email=email, data=data or {}, ts=_now(),
            ))

        def cancel() -> bool:
            return self._cancel_flags.get(job_id, False)

        def paused() -> bool:
            ev = self._pause_events.get(job_id)
            return ev is not None and not ev.is_set()

        # Persist researched dossiers to the same DB file the job store uses,
        # so the leads list/detail endpoints can read them.
        from app.lead_research.service import LeadResearchStore
        lead_store = LeadResearchStore(db_path=self._store._db_path)

        try:
            run_full(query, emit=emit, cancel=cancel, paused=paused, store=lead_store)
            with self._lock:
                job = self._store.get(job_id)
                if self._cancel_flags.get(job_id):
                    job.state = JobState.cancelled
                    logger.info("Job %s cancelled", job_id)
                else:
                    # A pause that races with natural completion: the job is
                    # done, so it is completed, not left paused forever.
                    self._pause_events[job_id].set()
                    job.state = JobState.completed
                    logger.info("Job %s completed (%d leads)", job_id, len(job.results))
                # Accumulate: a resumed (continued) job sums its active segments
                # instead of overwriting the earlier run's elapsed time.
                job.elapsed_s += (datetime.now(timezone.utc) - t0).total_seconds()
                job.updated_at = _now()
                self._store.save(job)
        except Exception as exc:  # noqa: BLE001 - record, don't kill the server
            logger.error("Job %s failed: %s", job_id, exc)
            with self._lock:
                job = self._store.get(job_id)
                job.state = JobState.failed
                job.error = str(exc)
                job.elapsed_s += (datetime.now(timezone.utc) - t0).total_seconds()
                job.updated_at = _now()
                self._store.save(job)
        finally:
            # Always clean up — completed, failed, or cancelled, the worker
            # thread is no longer live. sweep_dead_workers uses this ref.
            self._workers.pop(job_id, None)

    def _append_event(self, job_id: str, event: JobEvent) -> None:
        with self._lock:
            job = self._store.get(job_id)
            job.events.append(event)
            # Surface structured progress live: research outcomes -> results,
            # discovery pass summaries -> pass_log.
            if event.phase == "research" and event.data:
                job.results.append(event.data)
            elif event.phase == "discovery" and event.data and "pass" in event.data:
                job.pass_log.append(event.data)
            job.updated_at = _now()
            self._store.save(job)
