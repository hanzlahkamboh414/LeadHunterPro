"""Job runner — lifecycle, progress, cancel, failure (offline, no network)."""

from __future__ import annotations

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

def _fake_run_full_ok(query, emit=None, cancel=None, store=None, paused=None):
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
    def _boom(query, emit=None, cancel=None, store=None, paused=None):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr("app.leads.jobs.run_full", _boom)
    manager = JobManager(db_path=str(tmp_path / "jobs.db"))
    job = manager.submit(ResearchQuery(trade="gc", location="TX", target_emails=1))
    done = _wait_finished(manager, job.id)
    assert done.state is JobState.failed
    assert "pipeline exploded" in done.error


def test_job_cancel_graceful(tmp_path, monkeypatch):
    def _blocking(query, emit=None, cancel=None, store=None, paused=None):
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


def _block_on_pause(query, emit=None, cancel=None, store=None, paused=None):
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
    def _hold(query, emit=None, cancel=None, store=None, paused=None):
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
