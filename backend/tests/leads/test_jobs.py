"""Job runner — lifecycle, progress, cancel, failure (offline, no network)."""

from __future__ import annotations

import threading
import time

from app.leads.jobs import JobManager, JobStore
from app.leads.models import Job, JobState
from app.leads.pipeline import ResearchQuery


def _wait_finished(manager: JobManager, job_id: str, timeout: float = 5.0) -> Job:
    """Poll a job until it reaches a terminal state (or timeout).

    ``paused`` is NOT terminal — a paused job is still mid-run.
    """
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        job = manager.get(job_id)
        if job and job.state not in (JobState.queued, JobState.running, JobState.paused):
            return job
        time.sleep(0.01)
    raise TimeoutError(f"job {job_id} did not finish in {timeout}s (state={job.state})")


# ---------------------------------------------------------------------------
# JobStore roundtrip
# ---------------------------------------------------------------------------

def test_job_store_roundtrip(tmp_path):
    store = JobStore(db_path=str(tmp_path / "jobs.db"))
    job = Job(id="abc", query={"trade": "gc", "location": "TX", "target_emails": 1})
    store.save(job)
    got = store.get("abc")
    assert got.id == "abc"
    assert got.query["trade"] == "gc"
    assert got.state is JobState.queued

    job.state = JobState.completed
    job.results = [{"email": "a@x.com"}]
    store.save(job)
    got = store.get("abc")
    assert got.state is JobState.completed
    assert got.results[0]["email"] == "a@x.com"

    assert len(store.list_all()) == 1


# ---------------------------------------------------------------------------
# JobManager lifecycle
# ---------------------------------------------------------------------------

def _fake_run_full_ok(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
    if emit:
        emit("discovery", 1, 1, "pass 1", data={"pass": 1, "new_leads": 2, "total_leads": 2})
        emit("research", 1, 2, "lead 1", email="a@x.com",
             data={"email": "a@x.com", "score": 8.0, "recommendation": "contact_now"})
        emit("research", 2, 2, "lead 2", email="b@x.com",
             data={"email": "b@x.com", "score": 5.0, "recommendation": "nurture"})
    return {"leads_found": 2, "discovery_passes": [], "results": []}


def test_job_completes_with_progress(tmp_path, monkeypatch):
    monkeypatch.setattr("app.leads.jobs.run_full", _fake_run_full_ok)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    job = manager.submit(ResearchQuery(trade="gc", location="TX", target_emails=2))
    assert job.state is JobState.queued

    done = _wait_finished(manager, job.id)
    assert done.state is JobState.completed
    assert done.error == ""
    # live telemetry recorded: 1 discovery pass + 2 research leads
    phases = [e.phase for e in done.events]
    assert phases.count("discovery") == 1
    assert phases.count("research") == 2
    # structured results surfaced live
    assert len(done.results) == 2
    assert done.pass_log and done.pass_log[0]["pass"] == 1
    assert done.elapsed_s >= 0


def test_job_failure_records_error(tmp_path, monkeypatch):
    def _boom(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr("app.leads.jobs.run_full", _boom)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    job = manager.submit(ResearchQuery(trade="gc", location="TX", target_emails=1))
    done = _wait_finished(manager, job.id)
    assert done.state is JobState.failed
    assert "pipeline exploded" in done.error


def test_job_cancel_graceful(tmp_path, monkeypatch):
    def _blocking(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        while not (cancel and cancel()):
            time.sleep(0.005)
        return {"leads_found": 0, "discovery_passes": [], "results": []}

    monkeypatch.setattr("app.leads.jobs.run_full", _blocking)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    job = manager.submit(ResearchQuery(trade="gc", location="TX", target_emails=5))

    # wait until running, then cancel
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5.0:
        if manager.get(job.id).state is JobState.running:
            break
        time.sleep(0.01)
    assert manager.cancel(job.id) is True

    done = _wait_finished(manager, job.id)
    assert done.state is JobState.cancelled


def _block_on_pause(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
    """Emit discovery, then hold until the job is paused, then until resumed/cancelled.

    Waiting for the pause to actually be requested makes the test deterministic:
    the worker can never race ahead to completion before the test pauses it.
    """
    if emit:
        emit("discovery", 1, 1, "pass 1", data={"pass": 1, "new_leads": 1, "total_leads": 1})
    # Hold until pause is requested (deterministic control point).
    t0 = time.monotonic()
    while not (paused and paused()):
        if cancel and cancel():
            return {"leads_found": 0, "discovery_passes": [], "results": []}
        if time.monotonic() - t0 > 5:
            return {"leads_found": 0, "discovery_passes": [], "results": []}
        time.sleep(0.005)
    # Paused — block until resumed or cancelled.
    while paused and paused():
        if cancel and cancel():
            return {"leads_found": 0, "discovery_passes": [], "results": []}
        time.sleep(0.005)
    if emit:
        emit("research", 1, 1, "lead 1", email="a@x.com",
             data={"email": "a@x.com", "score": 8.0, "recommendation": "contact_now"})
    return {"leads_found": 1, "discovery_passes": [], "results": []}


def _wait_running(manager, job_id, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if manager.get(job_id).state is JobState.running:
            return
        time.sleep(0.01)
    raise TimeoutError(f"job {job_id} never reached running")


def test_job_resume_failed_orphan_continues_same_id(tmp_path, monkeypatch):
    """A search interrupted by a server restart (orphan failed at boot) can be
    CONTINUED on the same job id: a fresh worker re-runs the query while the
    already-accumulated results/events are preserved — 'wahi se continue'."""
    monkeypatch.setattr("app.leads.jobs.run_full", _fake_run_full_ok)
    store = JobStore(db_path=str(tmp_path / "jobs.db"))
    # Simulate the row recover_orphans leaves at boot: failed, no worker, the
    # restart-interruption message, and PARTIAL results from before the restart.
    job = Job(
        id="orphan1",
        query={"trade": "gc", "location": "TX", "target_emails": 2},
        state=JobState.failed,
        error="interrupted by server restart; re-run to continue",
        results=[{"email": "prior@x.com", "score": 6.0, "recommendation": "nurture"}],
        created_at="2026-09-06T02:18:53",
        updated_at="2026-09-06T02:18:53",
    )
    store.save(job)

    manager = JobManager(store=store)
    assert manager.get("orphan1").state is JobState.failed
    assert manager.resume("orphan1") is True
    assert manager.get("orphan1").state is JobState.running

    done = _wait_finished(manager, "orphan1")
    assert done.state is JobState.completed
    assert done.error == ""
    # Continuity: the pre-restart result is STILL there, the new ones append.
    assert len(done.results) == 3
    assert done.results[0]["email"] == "prior@x.com"
    keys = [r["email"] for r in done.results if r.get("email")]
    assert {"a@x.com", "b@x.com", "prior@x.com"} == set(keys)
    # Events keep flowing too: 1 discovery + 2 research after the resume.
    assert len(done.events) == 3


def test_job_resumed_orphan_can_be_cancelled(tmp_path, monkeypatch):
    """A re-launched (continued) worker re-arms cancel: the user can stop it."""
    def _hold(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        while not (cancel and cancel()):
            time.sleep(0.005)
        return {"leads_found": 0, "discovery_passes": [], "results": []}

    monkeypatch.setattr("app.leads.jobs.run_full", _hold)
    store = JobStore(db_path=str(tmp_path / "jobs.db"))
    job = Job(
        id="o2",
        query={"trade": "gc", "location": "TX", "target_emails": 3},
        state=JobState.failed,
        error="interrupted by server restart; re-run to continue",
        created_at="2026-09-06T02:20:00",
        updated_at="2026-09-06T02:20:00",
    )
    store.save(job)
    manager = JobManager(store=store)
    assert manager.resume("o2") is True
    _wait_running(manager, "o2")
    assert manager.cancel("o2") is True
    done = _wait_finished(manager, "o2")
    assert done.state is JobState.cancelled


def test_job_resume_rejects_completed_cancelled_and_unknown(tmp_path):
    """Only paused/interrupted (failed) jobs may be resumed/continued."""
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    assert manager.resume("ghost") is False

    store = manager._store
    for state in (JobState.completed, JobState.cancelled):
        jid = state.value
        store.save(Job(id=jid, query={"trade": "gc", "location": "TX", "target_emails": 1},
                       state=state))
        assert manager.resume(jid) is False


def test_job_pause_then_resume(tmp_path, monkeypatch):
    monkeypatch.setattr("app.leads.jobs.run_full", _block_on_pause)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    job = manager.submit(ResearchQuery(trade="gc", location="TX", target_emails=1))
    _wait_running(manager, job.id)

    assert manager.pause(job.id) is True
    assert manager.get(job.id).state is JobState.paused
    time.sleep(0.05)
    # worker is blocked: still paused, not completed
    assert manager.get(job.id).state is JobState.paused

    assert manager.resume(job.id) is True
    done = _wait_finished(manager, job.id)
    assert done.state is JobState.completed
    assert len(done.results) == 1


def test_job_cancel_while_paused(tmp_path, monkeypatch):
    monkeypatch.setattr("app.leads.jobs.run_full", _block_on_pause)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    job = manager.submit(ResearchQuery(trade="gc", location="TX", target_emails=1))
    _wait_running(manager, job.id)

    assert manager.pause(job.id) is True
    assert manager.cancel(job.id) is True
    done = _wait_finished(manager, job.id)
    assert done.state is JobState.cancelled


def test_job_validation_rejects_bad_query(tmp_path):
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    try:
        manager.submit(ResearchQuery(trade="", location="", target_emails=0))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_list_jobs_returns_submitted(tmp_path, monkeypatch):
    monkeypatch.setattr("app.leads.jobs.run_full", _fake_run_full_ok)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    manager.submit(ResearchQuery(trade="gc", location="TX", target_emails=2))
    assert len(manager.list_jobs()) >= 1


# ---------------------------------------------------------------------------
# Dead-worker sweep: "front abi b running show ho rhi ha" fix
# ---------------------------------------------------------------------------

def test_sweep_marks_dead_worker_job_failed_on_read(tmp_path):
    """A job whose worker thread died (process still alive) is honestly marked
    failed the moment it is read — the DB row must never say 'running' forever."""
    store = JobStore(db_path=str(tmp_path / "jobs.db"))
    store.save(Job(
        id="dead1",
        query={"trade": "gc", "location": "TX", "target_emails": 2},
        state=JobState.running,
        created_at="2026-09-07T02:00:00",
        updated_at="2026-09-07T02:00:10",
    ))
    manager = JobManager(store=store)

    # Simulate a worker that already exited (started + finished => not alive).
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    assert not dead.is_alive()
    manager._workers["dead1"] = dead  # this process "owns" the dead worker

    got = manager.get("dead1")
    assert got.state is JobState.failed
    assert "Continue" in got.error
    assert "dead1" not in manager._workers  # ref cleaned up
    # Idempotent: a second read does not change anything.
    assert manager.get("dead1").state is JobState.failed


def test_sweep_leaves_live_worker_jobs_untouched(tmp_path, monkeypatch):
    """A genuinely-running job (worker thread alive) is never swept."""
    def _hold(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        while True:
            time.sleep(0.005)

    monkeypatch.setattr("app.leads.jobs.run_full", _hold)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    job = manager.submit(ResearchQuery(trade="gc", location="TX", target_emails=1))
    _wait_running(manager, job.id)

    assert manager._workers[job.id].is_alive()
    assert manager.get(job.id).state is JobState.running
    assert manager.sweep_dead_workers() == 0


def test_sweep_recovers_via_list_jobs_too(tmp_path):
    """History uses list_jobs; an unread dead-worker job must surface as failed."""
    store = JobStore(db_path=str(tmp_path / "jobs.db"))
    store.save(Job(
        id="dead2",
        query={"trade": "gc", "location": "TX", "target_emails": 2},
        state=JobState.queued,
        created_at="2026-09-07T02:05:00",
        updated_at="2026-09-07T02:05:00",
    ))
    manager = JobManager(store=store)
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    manager._workers["dead2"] = dead

    jobs = manager.list_jobs()
    got = next(j for j in jobs if j.id == "dead2")
    assert got.state is JobState.failed
    assert "Continue" in got.error


# ---------------------------------------------------------------------------
# Honest accounting on interrupt (§6)
# ---------------------------------------------------------------------------

def _han_results():
    """Research results a run accumulates via _append_event before an interrupt
    lands — 2 contact_now (working), 1 nurture, 1 skip."""
    return [
        {"email": "w1@x.com", "recommendation": "contact_now", "working": True},
        {"email": "w2@x.com", "recommendation": "contact_now", "working": True},
        {"email": "n1@x.com", "recommendation": "nurture", "working": False},
        {"email": "s1@x.com", "recommendation": "skip", "working": False},
    ]


def test_recover_orphans_persists_real_delivery_counts(tmp_path):
    """PROOF (§6 dishonest accounting): an orphaned run that had already
    researched 4 leads (2 working) must NOT be failed with leads_found=0 /
    working_leads=0 — the interrupted run's REAL delivery survives the boot
    sweep, so History shows "4 found / 2 working" not a false zero."""
    store = JobStore(db_path=str(tmp_path / "jobs.db"))
    store.save(Job(
        id="orphan_working",
        query={"trade": "gc", "location": "Houston TX", "target_emails": 200},
        state=JobState.running,
        created_at="2026-09-10T09:06:00",
        updated_at="2026-09-10T11:09:00",
        results=_han_results(),
    ))
    fresh = JobManager(store=store)
    assert fresh.recover_orphans() == 1
    got = fresh.get("orphan_working")
    assert got.state is JobState.failed
    assert got.leads_found == 4      # every researched lead counts
    assert got.working_leads == 2    # contact_now = working


def test_sweep_dead_worker_persists_real_delivery_counts(tmp_path):
    """An on-the-fly dead-worker sweep (thread died mid-run) keeps the run's
    real counts instead of the DB's zeros."""
    store = JobStore(db_path=str(tmp_path / "jobs.db"))
    store.save(Job(
        id="dead_working",
        query={"trade": "gc", "location": "TX", "target_emails": 50},
        state=JobState.running,
        created_at="2026-09-10T10:00:00",
        updated_at="2026-09-10T10:01:00",
        results=_han_results(),
    ))
    manager = JobManager(store=store)
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    manager._workers["dead_working"] = dead

    assert manager.sweep_dead_workers() == 1
    got = manager.get("dead_working")
    assert got.state is JobState.failed
    assert got.leads_found == 4
    assert got.working_leads == 2


def test_exception_mid_run_persists_real_delivery_counts(tmp_path, monkeypatch):
    """An exception thrown after research has started must not erase what was
    already delivered — the failed job shows its real partial counts."""
    def _boom(query, emit=None, cancel=None, paused=None, store=None, user_id=""):
        emit("research", 1, 1, "lead 1", email="w1@x.com",
             data={"email": "w1@x.com", "recommendation": "contact_now", "working": True})
        raise RuntimeError("simulated mid-run failure")

    monkeypatch.setattr("app.leads.jobs.run_full", _boom)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    job = manager.submit(ResearchQuery(trade="gc", location="TX", target_emails=1))
    done = _wait_finished(manager, job.id)
    assert done.state is JobState.failed
    assert done.leads_found == 1
    assert done.working_leads == 1


# ---------------------------------------------------------------------------
# Phase 4 admission control — bounded concurrent pipelines (MAX_ACTIVE_JOBS).
# 10 simultaneous users must queue behind the search throttle's drain
# capacity instead of bursting past its 30s budget (breaker-trip failure).
# ---------------------------------------------------------------------------

def _wait_until(cond, timeout: float = 5.0, what: str = "condition") -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return
        time.sleep(0.01)
    raise TimeoutError(f"{what} not met in {timeout}s")


def _has_queue_event(manager: JobManager, job_id: str) -> bool:
    return any(e.phase == "queue" for e in manager.get(job_id).events)


def test_admission_control_queues_extra_jobs(tmp_path, monkeypatch):
    """With the cap at 2, a third simultaneous job stays HONESTLY queued (with
    a queue event telling the frontend why) and auto-starts when a slot frees."""
    from app.core import config
    monkeypatch.setattr(config.settings, "MAX_ACTIVE_JOBS", 2)
    release = {t: threading.Event() for t in "abc"}
    started = {t: threading.Event() for t in "abc"}

    def _hold(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        started[query.trade].set()
        release[query.trade].wait(timeout=10)
        return {"leads_found": 0, "discovery_passes": [], "results": []}

    monkeypatch.setattr("app.leads.jobs.run_full", _hold)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    jobs = {t: manager.submit(ResearchQuery(trade=t, location="TX", target_emails=1))
            for t in "abc"}

    # Exactly two run (whichever two); the third waits in the queue.
    _wait_until(lambda: sum(e.is_set() for e in started.values()) == 2,
                what="two jobs admitted")
    waiting = next(t for t in "abc" if not started[t].is_set())
    assert manager.get(jobs[waiting].id).state is JobState.queued
    assert _has_queue_event(manager, jobs[waiting].id)  # honest "why" for the UI

    # Free one slot -> the queued job starts automatically.
    running = next(t for t in "abc" if started[t].is_set())
    release[running].set()
    _wait_until(lambda: started[waiting].is_set(), what="queued job admitted")
    assert manager.get(jobs[waiting].id).state is JobState.running

    for t in "abc":
        release[t].set()
    for t in "abc":
        assert _wait_finished(manager, jobs[t].id).state is JobState.completed


def test_admission_cancel_while_queued_never_runs(tmp_path, monkeypatch):
    """A job cancelled while waiting in the queue is marked cancelled WITHOUT
    ever entering the pipeline — no slot taken, no searches burned."""
    from app.core import config
    monkeypatch.setattr(config.settings, "MAX_ACTIVE_JOBS", 1)
    release = threading.Event()
    ran = {"n": 0}

    def _hold(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        ran["n"] += 1
        release.wait(timeout=10)
        return {"leads_found": 0, "discovery_passes": [], "results": []}

    monkeypatch.setattr("app.leads.jobs.run_full", _hold)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    j1 = manager.submit(ResearchQuery(trade="a", location="TX", target_emails=1))
    j2 = manager.submit(ResearchQuery(trade="b", location="TX", target_emails=1))

    # One runs, one queues (whichever — the test is order-independent).
    _wait_until(lambda: ran["n"] == 1, what="one job admitted")
    queued, running = (
        (j2, j1) if manager.get(j2.id).state is JobState.queued else (j1, j2)
    )
    assert manager.get(running.id).state is JobState.running

    # Cancel the queued job: cancelled without running.
    assert manager.cancel(queued.id) is True
    done = _wait_finished(manager, queued.id)
    assert done.state is JobState.cancelled

    # The running job finishes; the cancelled one NEVER entered the pipeline.
    release.set()
    assert _wait_finished(manager, running.id).state is JobState.completed
    assert ran["n"] == 1


def test_admission_slot_released_on_failure(tmp_path, monkeypatch):
    """A crashed pipeline must not leak its slot — the queued job behind it
    still runs."""
    from app.core import config
    monkeypatch.setattr(config.settings, "MAX_ACTIVE_JOBS", 1)
    started_b = threading.Event()
    release_b = threading.Event()

    def _flaky(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        if query.trade == "a":
            raise RuntimeError("pipeline exploded")
        started_b.set()
        release_b.wait(timeout=10)
        return {"leads_found": 0, "discovery_passes": [], "results": []}

    monkeypatch.setattr("app.leads.jobs.run_full", _flaky)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    j1 = manager.submit(ResearchQuery(trade="a", location="TX", target_emails=1))
    j2 = manager.submit(ResearchQuery(trade="b", location="TX", target_emails=1))

    # Whichever order admission picked, BOTH must reach a terminal state and
    # BOTH must have run (the failure freed its slot for the other).
    d1 = _wait_finished(manager, j1.id)
    _wait_until(started_b.is_set, what="second job admitted after failure")
    release_b.set()
    d2 = _wait_finished(manager, j2.id)
    assert {d1.state, d2.state} == {JobState.failed, JobState.completed}
    assert "pipeline exploded" in (d1.error or d2.error)


def test_admission_disabled_when_zero(tmp_path, monkeypatch):
    """MAX_ACTIVE_JOBS=0 is the kill-switch: every job runs immediately (the
    pre-Phase-4 behavior)."""
    from app.core import config
    monkeypatch.setattr(config.settings, "MAX_ACTIVE_JOBS", 0)
    started = {t: threading.Event() for t in "abc"}
    release = threading.Event()

    def _hold(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        started[query.trade].set()
        release.wait(timeout=10)
        return {"leads_found": 0, "discovery_passes": [], "results": []}

    monkeypatch.setattr("app.leads.jobs.run_full", _hold)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    jobs = {t: manager.submit(ResearchQuery(trade=t, location="TX", target_emails=1))
            for t in "abc"}

    # All three run at once — no admission wait, no queue events.
    _wait_until(lambda: all(e.is_set() for e in started.values()),
                what="all three jobs running concurrently")
    assert not any(_has_queue_event(manager, jobs[t].id) for t in "abc")
    release.set()
    for t in "abc":
        assert _wait_finished(manager, jobs[t].id).state is JobState.completed


def test_admission_fifo_first_come_first_served(tmp_path, monkeypatch):
    """Slots are handed out in submit order: while job B waits (queue event
    observed), a later job C submitted behind it cannot jump the line."""
    from app.core import config
    monkeypatch.setattr(config.settings, "MAX_ACTIVE_JOBS", 1)
    release = {t: threading.Event() for t in "abc"}
    started = {t: threading.Event() for t in "abc"}

    def _hold(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        started[query.trade].set()
        release[query.trade].wait(timeout=10)
        return {"leads_found": 0, "discovery_passes": [], "results": []}

    monkeypatch.setattr("app.leads.jobs.run_full", _hold)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    ja = manager.submit(ResearchQuery(trade="a", location="TX", target_emails=1))
    _wait_until(lambda: manager.get(ja.id).state is JobState.running,
                what="job a admitted")

    jb = manager.submit(ResearchQuery(trade="b", location="TX", target_emails=1))
    # Wait until B is REGISTERED as a waiter (its queue event exists) BEFORE
    # submitting C — that makes B provably ahead in the waiter queue.
    _wait_until(lambda: _has_queue_event(manager, jb.id), what="job b queued")
    jc = manager.submit(ResearchQuery(trade="c", location="TX", target_emails=1))

    release["a"].set()
    # B (first in line) starts; C is still queued.
    _wait_until(lambda: started["b"].is_set(), what="job b admitted in FIFO order")
    assert not started["c"].is_set()
    assert manager.get(jc.id).state is JobState.queued

    release["b"].set()
    _wait_until(lambda: started["c"].is_set(), what="job c admitted")
    release["c"].set()
    for j in (ja, jb, jc):
        assert _wait_finished(manager, j.id).state is JobState.completed
