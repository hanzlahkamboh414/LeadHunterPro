"""Leads API — end-to-end over HTTP (TestClient, offline fakes)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

import app.api.v1.leads as leads_module
from app.api.v1.leads import JobManager, LeadResearchStore
from app.lead_research.agent import DEAD_DOMAIN_MARKER
from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.lead_research.service import PendingLeadsStore
from app.leads.models import Job, JobState
from app.main import app


def _dossier(email: str, domain: str, score: float = 8.0,
             rec: str = "contact_now", bound: bool = True,
             person: str = "Jane", company: str = "Acme",
             role: str = "Owner", location: str = "Texas") -> LeadDossier:
    # location defaults non-empty: R2 makes contact_now impossible without a
    # verified geolocation, and these fixtures stand for genuinely-qualified
    # dossiers (the same rule that demotes nsarro@unitedcr.com to nurture).
    return LeadDossier(
        email=email,
        domain=domain,
        company=CompanyProfile(name=company, industry="general contractor", location=location),
        person=PersonFindings(name=person, role=role, bound=bound, role_relevance=True),
        potential_score=score,
        recommendation=rec,
    )


def _dead_domain(email: str, domain: str) -> LeadDossier:
    """A dossier rejected at the dead-domain MX gate (the user's complaint: such
    dossiers used to sit in totals and the leads list)."""
    d = _dossier(email, domain, score=0.0, rec="skip", bound=False)
    d.fit = f"Dead/expired domain — {DEAD_DOMAIN_MARKER} (undeliverable)"
    return d


def _wait_state(client, job_id, want, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = client.get(f"/api/v1/leads/jobs/{job_id}")
        if r.status_code == 200 and r.json()["state"] == want:
            return r.json()
        time.sleep(0.01)
    raise TimeoutError(f"job {job_id} did not reach {want}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _setup(tmp_path, monkeypatch, run_full=None, admin=False):
    """Point the router's manager + store at a tmp DB; optionally fake the pipeline.

    Every test client is minted a JWT for a ``testuser`` (admin=True promotes
    them).  The store's ``save()`` is wrapped so that all seeded dossiers carry
    ``user_id=testuser.id`` — under strict isolation the test user can only see
    rows that belong to them.
    """
    if run_full is not None:
        monkeypatch.setattr("app.leads.jobs.run_full", run_full)
    # --- Create the test user FIRST so we know the user_id for store seeds. ---
    from app.auth.jwt import create_access_token
    from app.auth.models import UserStore
    from app.auth.activity import ActivityStore
    import app.auth.dependencies as deps
    import app.auth.activity as activity_module
    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    # create_job records "search" activity via the get_activity() singleton —
    # point it at the tmp db too, or every test run pollutes the real one.
    monkeypatch.setattr(
        activity_module, "_activity_store", ActivityStore(db_path=str(tmp_path / "users.db"))
    )
    user = user_store.create("testuser", "test@example.com", "password")
    if admin:
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "users.db"))
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user.id,))
        conn.commit()
        conn.close()
        user = user_store.get_by_username("testuser")
        assert user is not None and user.is_admin
    # Wrap the store so save() auto-injects user_id for seeded dossiers.
    _real_store = LeadResearchStore(db_path=str(tmp_path / "jobs.db"))
    _orig_save = _real_store.save
    _real_store.save = lambda d, **kw: _orig_save(d, user_id=kw.pop("user_id", "") or user.id)
    # Manager: a Job seeded directly in a test inherits the test user's id when
    # left blank, so user-scoped views (the sources dropdown) can see it.
    _mgr = JobManager(db_path=str(tmp_path / "jobs.db"))
    _orig_job_save = _mgr._store.save
    def _job_save(job):
        if not getattr(job, "user_id", ""):
            job.user_id = user.id
        return _orig_job_save(job)
    _mgr._store.save = _job_save
    monkeypatch.setattr(leads_module, "_manager", _mgr)
    monkeypatch.setattr(leads_module, "_store", _real_store)
    token = create_access_token(user.id, user.is_admin, username=user.username)
    return TestClient(
        app,
        headers={"Authorization": f"Bearer {token}"},
    )


# ---------------------------------------------------------------------------
# Job lifecycle over HTTP
# ---------------------------------------------------------------------------

def test_recover_orphans_marks_dead_inflight_jobs(tmp_path):
    """PROOF (root-cause fix): a job left running by a killed process must be
    failed at the next boot — otherwise History shows "Running" forever."""
    db = str(tmp_path / "jobs.db")
    prev = JobManager(db_path=db)
    # Jobs a PREVIOUS process left in a live state (e.g. uvicorn killed mid-run
    # / TaskStop). No worker thread of the next process can ever advance them.
    for jid, state in (("orphan1", JobState.running), ("orphan2", JobState.queued)):
        prev._store.save(Job(
            id=jid,
            query={"trade": "general contractor", "location": "Texas", "target_emails": 1},
            state=state,
            created_at="2026-09-04T10:00:00",
            updated_at="2026-09-04T10:00:00",
        ))
    # A completed job is cross-run state and must stay untouched.
    prev._store.save(Job(
        id="done",
        query={"trade": "general contractor", "location": "Texas", "target_emails": 1},
        state=JobState.completed,
        elapsed_s=120.0,
        created_at="2026-09-04T09:00:00",
        updated_at="2026-09-04T09:02:00",
    ))

    # A NEW process boots against the same DB.
    fresh = JobManager(db_path=db)
    recovered = fresh.recover_orphans()

    assert recovered == 2
    o1 = fresh.get("orphan1")
    o2 = fresh.get("orphan2")
    assert o1.state is JobState.failed
    assert "interrupted by server restart" in o1.error
    # Honest lifetime recorded even though no worker thread could write it.
    assert o1.elapsed_s > 0
    assert o2.state is JobState.failed
    assert "interrupted by server restart" in o2.error
    # Cross-run state never swept.
    assert fresh.get("done").state is JobState.completed
    assert fresh.get("done").elapsed_s == 120.0
    # Idempotent: a second boot finds nothing left to recover.
    assert fresh.recover_orphans() == 0


def test_create_job_and_poll_progress(tmp_path, monkeypatch):
    def fake(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        emit("discovery", 1, 1, "pass 1", data={"pass": 1, "new_leads": 1, "total_leads": 1})
        emit("research", 1, 1, "lead 1", email="a@x.com",
             data={"email": "a@x.com", "score": 8.0, "recommendation": "contact_now"})

    client = _setup(tmp_path, monkeypatch, run_full=fake)

    r = client.post("/api/v1/leads/jobs", json={
        "trade": "general contractor", "location": "Texas", "target_emails": 1,
    })
    assert r.status_code == 201
    job = r.json()
    assert job["state"] == "queued"
    assert job["query"]["trade"] == "general contractor"

    done = _wait_state(client, job["id"], "completed")
    assert done["state"] == "completed"
    assert len(done["events"]) == 2
    assert len(done["results"]) == 1
    assert done["results"][0]["recommendation"] == "contact_now"


def test_job_completed_surfaces_honest_outcome(tmp_path, monkeypatch):
    """H1: a run that under-delivers must say so. The seam that used to discard
    run_full's outcome now stamps the job + appends an honest terminal event —
    a 4/500 run never reads as a clean "Completed" again (§6). Run as ADMIN:
    500-target runs are admin-sized (users are capped at 150)."""
    def fake(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        return {"working_leads": 4, "leads_found": 17, "shortfall": 496,
                "shortfall_reason": "discovery_exhausted"}

    client = _setup(tmp_path, monkeypatch, run_full=fake, admin=True)
    r = client.post("/api/v1/leads/jobs", json={
        "trade": "general contractor", "location": "Texas", "target_emails": 500,
    })
    assert r.status_code == 201
    job = _wait_state(client, r.json()["id"], "completed")
    assert job["working_leads"] == 4
    assert job["leads_found"] == 17
    assert job["shortfall"] == 496
    assert job["shortfall_reason"] == "discovery_exhausted"
    # The terminal event tells the honest story (H3's data source): the run
    # completed with REAL delivery, not a stale mid-run "step X/target".
    last = job["events"][-1]
    assert last["phase"] == "result"
    assert "4 of 500 working" in last["message"]
    assert "discovery_exhausted" in last["message"]


def test_job_store_roundtrips_outcome_fields(tmp_path):
    """H1: the job store persists the honest-outcome columns (additive), so a
    completed run's real delivery survives reload and shows in History."""
    from app.leads.jobs import JobStore

    store = JobStore(db_path=str(tmp_path / "jobs.db"))
    job = Job(
        id="abc123",
        query={"trade": "gc", "location": "TX", "target_emails": 500},
        state=JobState.completed,
        created_at="2026-09-09T10:00:00",
        updated_at="2026-09-09T10:00:00",
        working_leads=4, leads_found=17, shortfall=496,
        shortfall_reason="discovery_exhausted",
    )
    store.save(job)
    loaded = store.get("abc123")
    assert loaded.working_leads == 4
    assert loaded.leads_found == 17
    assert loaded.shortfall == 496
    assert loaded.shortfall_reason == "discovery_exhausted"
    assert [j.working_leads for j in store.list_all()] == [4]


def test_job_store_alters_legacy_db_with_outcome_columns(tmp_path):
    """H1 additive migration: a jobs table created BEFORE the outcome columns
    gains them via the guarded ALTER — pre-existing rows read back valid, and
    the new fields persist once written."""
    import sqlite3

    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            query_json TEXT NOT NULL, state TEXT NOT NULL,
            events_json TEXT NOT NULL, results_json TEXT NOT NULL,
            pass_log_json TEXT NOT NULL, error TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            elapsed_s REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    from app.leads.jobs import JobStore

    store = JobStore(db_path=db)  # must ALTER in place without error
    job = Job(
        id="legacy1",
        query={"trade": "gc", "location": "TX", "target_emails": 10},
        state=JobState.completed,
        created_at="2026-09-09T10:00:00",
        updated_at="2026-09-09T10:00:00",
        working_leads=10, leads_found=12, shortfall=0, shortfall_reason="",
    )
    store.save(job)
    loaded = store.get("legacy1")
    assert loaded.working_leads == 10
    assert loaded.shortfall == 0
    assert loaded.shortfall_reason == ""


def test_create_job_carries_search_name_and_folder(tmp_path, monkeypatch):
    """The Execute form's optional name+folder ride on the job's query, so the
    pipeline can auto-file leads of THIS run — the Phase C "mix ni hogi" hook."""
    seen = {}

    def fake(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        seen["query"] = query
        return {}

    client = _setup(tmp_path, monkeypatch, run_full=fake)
    r = client.post("/api/v1/leads/jobs", json={
        "trade": "general contractor", "location": "Houston TX",
        "target_emails": 2, "search_name": "Houston GC Q3",
        "folder": "Q3 Outreach",
    })
    assert r.status_code == 201
    # The job's own query dict carries the fields (frontend echo contract).
    assert r.json()["query"]["search_name"] == "Houston GC Q3"
    assert r.json()["query"]["folder"] == "Q3 Outreach"
    # And the worker received them on the ResearchQuery it runs.
    _wait_state(client, r.json()["id"], "completed")
    assert seen["query"].search_name == "Houston GC Q3"
    assert seen["query"].folder == "Q3 Outreach"


def test_validation_rejects_bad_query(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    r = client.post("/api/v1/leads/jobs", json={"trade": "", "location": "", "target_emails": 0})
    assert r.status_code == 422


def test_get_job_404(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    r = client.get("/api/v1/leads/jobs/nope")
    assert r.status_code == 404


def test_cancel_job(tmp_path, monkeypatch):
    def blocking(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        while not (cancel and cancel()):
            time.sleep(0.005)
        return {}

    client = _setup(tmp_path, monkeypatch, run_full=blocking)
    job = client.post("/api/v1/leads/jobs", json={
        "trade": "gc", "location": "TX", "target_emails": 5,
    }).json()
    r = client.post(f"/api/v1/leads/jobs/{job['id']}/cancel")
    assert r.status_code == 200
    done = _wait_state(client, job["id"], "cancelled")
    assert done["state"] == "cancelled"


def _pause_blocking(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
    if emit:
        emit("discovery", 1, 1, "pass 1", data={"pass": 1, "new_leads": 1, "total_leads": 1})
    # Hold until pause is requested (deterministic control point).
    t0 = time.monotonic()
    while not (paused and paused()):
        if cancel and cancel():
            return {}
        if time.monotonic() - t0 > 5:
            return {}
        time.sleep(0.005)
    # Paused — block until resumed or cancelled.
    while paused and paused():
        if cancel and cancel():
            return {}
        time.sleep(0.005)
    if emit:
        emit("research", 1, 1, "lead 1", email="a@x.com",
             data={"email": "a@x.com", "score": 8.0, "recommendation": "contact_now"})
    return {}


def test_pause_then_resume_over_http(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch, run_full=_pause_blocking)
    job = client.post("/api/v1/leads/jobs", json={
        "trade": "gc", "location": "TX", "target_emails": 1,
    }).json()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5:
        if client.get(f"/api/v1/leads/jobs/{job['id']}").json()["state"] == "running":
            break
        time.sleep(0.01)
    r = client.post(f"/api/v1/leads/jobs/{job['id']}/pause")
    assert r.status_code == 200
    assert r.json()["state"] == "paused"
    assert client.get(f"/api/v1/leads/jobs/{job['id']}").json()["state"] == "paused"
    r = client.post(f"/api/v1/leads/jobs/{job['id']}/resume")
    assert r.status_code == 200
    done = _wait_state(client, job["id"], "completed")
    assert done["state"] == "completed"
    assert len(done["results"]) == 1


# ---------------------------------------------------------------------------
# Leads list + detail
# ---------------------------------------------------------------------------

def test_list_leads_filters(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com", score=8.0, rec="contact_now", bound=True))
    store.save(_dossier("b@y.com", "y.com", score=3.0, rec="nurture", bound=False, person=""))

    # all
    r = client.get("/api/v1/leads")
    assert r.status_code == 200
    assert len(r.json()) == 2

    # filter: contact_now only
    r = client.get("/api/v1/leads", params={"recommendation": "contact_now"})
    assert [l["email"] for l in r.json()] == ["a@x.com"]

    # filter: bound + min_score
    r = client.get("/api/v1/leads", params={"bound": "true", "min_score": 5})
    assert [l["email"] for l in r.json()] == ["a@x.com"]


def test_regate_demotes_stale_ai_era_rec(tmp_path, monkeypatch):
    """An old dossier stored as contact_now under an AI-era (irrelevant) role is
    served as nurture — the deterministic list is the authority at READ time."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(
        _dossier("michael@ferguson.com", "ferguson.com", score=8.0,
                 rec="contact_now", role="Sales Representative, Ferguson Water Works")
    )
    r = client.get("/api/v1/leads")
    lead = r.json()[0]
    assert lead["recommendation"] == "nurture"


def test_list_leads_default_hides_skip_and_orders_actionable_first(tmp_path, monkeypatch):
    """Default view = ACTIONABLE ONLY, in discovery order (insertion order,
    rowid ASC — the order leads were found, stable across refresh — the user's
    "leads jump to the bottom after refresh" complaint). Dead-domain / skip
    dossiers are hidden (they never inflate the count) and can't appear here."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("high@x.com", "x.com", score=8.0, rec="contact_now"))
    store.save(_dossier("low@x.com", "x.com", score=6.0, rec="contact_now"))
    # nurture saved LAST = newest, and OUTSCORES both — discovery order keeps it
    # last regardless (no tier/score re-sort on refresh).
    store.save(_dossier("nurture@y.com", "y.com", score=8.5, rec="nurture", bound=False, person="Bo"))
    store.save(_dead_domain("dead@gone.com", "gone.com"))
    store.save(_dossier("junk@z.com", "z.com", score=2.0, rec="skip", bound=False))

    r = client.get("/api/v1/leads")
    assert r.status_code == 200
    emails = [l["email"] for l in r.json()]
    assert emails == ["high@x.com", "low@x.com", "nurture@y.com"], emails


def test_list_leads_skip_filter_reveals_junk_in_discovery_order(tmp_path, monkeypatch):
    """?recommendation=skip still reveals junk — in discovery order (insertion
    order, rowid ASC) — for the user who explicitly wants to inspect it."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com", score=8.0, rec="contact_now"))
    store.save(_dead_domain("dead@gone.com", "gone.com"))
    store.save(_dossier("junk@z.com", "z.com", score=2.0, rec="skip", bound=False))

    r = client.get("/api/v1/leads", params={"recommendation": "skip"})
    assert [l["email"] for l in r.json()] == ["dead@gone.com", "junk@z.com"]


def test_leads_carry_source_run_and_filter(tmp_path, monkeypatch):
    """Each lead names the query run that produced it, and can be filtered by
    it — a fresh search's leads are separated from older runs (no more mix)."""
    def fake(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        # Mirror the real pipeline: persist under the job's user_id so the owner
        # (this test's non-admin user) can see the leads under strict isolation.
        store.save(_dossier("a@x.com", "x.com"), user_id=user_id)
        store.save(_dossier("b@y.com", "y.com", score=4.0, rec="nurture", bound=False, person="Bo"), user_id=user_id)
        emit("research", 1, 1, "lead 1", email="a@x.com",
             data={"email": "a@x.com", "score": 8.0, "recommendation": "contact_now"})
        emit("research", 2, 2, "lead 2", email="b@y.com",
             data={"email": "b@y.com", "score": 4.0, "recommendation": "nurture"})

    client = _setup(tmp_path, monkeypatch, run_full=fake)
    job = client.post("/api/v1/leads/jobs", json={
        "trade": "Roofing", "location": "Dallas TX", "target_emails": 1,
    }).json()
    _wait_state(client, job["id"], "completed")

    leads = client.get("/api/v1/leads").json()
    sources = {l["email"]: l["source"] for l in leads}
    assert sources["a@x.com"] == "Roofing · Dallas TX", sources
    assert sources["b@y.com"] == "Roofing · Dallas TX", sources

    # filter by this run only
    r = client.get("/api/v1/leads", params={"source": "Roofing · Dallas TX"})
    assert len(r.json()) == 2


def test_get_lead_detail(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    leads_module._store.save(_dossier("a@x.com", "x.com", person="Jane"))
    r = client.get("/api/v1/leads/a@x.com")
    assert r.status_code == 200
    d = r.json()
    assert d["email"] == "a@x.com"
    assert d["person"]["name"] == "Jane"
    assert d["company"]["name"] == "Acme"
    assert d["recommendation"] == "contact_now"


def test_get_lead_404(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    r = client.get("/api/v1/leads/missing@x.com")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def test_export_csv(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com", score=8.0, rec="contact_now", person="Jane"))
    store.save(_dossier("b@y.com", "y.com", score=3.0, rec="nurture", bound=False, person=""))

    r = client.get("/api/v1/leads/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    body = r.text
    assert body.startswith("email,name,company")
    assert "a@x.com" in body
    assert "Jane" in body
    assert "b@y.com" in body

    # filtered export: contact_now only
    r2 = client.get("/api/v1/leads/export.csv", params={"recommendation": "contact_now"})
    assert "a@x.com" in r2.text
    assert "b@y.com" not in r2.text


def test_export_csv_empty(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    r = client.get("/api/v1/leads/export.csv")
    assert r.status_code == 200
    # header row only, no data rows
    assert r.text.strip() == "email,name,company"
    assert len(r.text.strip().splitlines()) == 1


def test_export_csv_default_excludes_skip(tmp_path, monkeypatch):
    """Default CSV = ACTIONABLE ONLY — a dead domain is not an outreach lead
    and its row must not land on an emailed list. ?recommendation=skip exports
    junk explicitly, the same honest filter the leads list applies."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com", score=8.0, rec="contact_now", person="Jane"))
    store.save(_dead_domain("dead@gone.com", "gone.com"))

    r = client.get("/api/v1/leads/export.csv")
    assert "a@x.com" in r.text
    assert "dead@gone.com" not in r.text  # skip hidden — count stays honest

    r2 = client.get("/api/v1/leads/export.csv", params={"recommendation": "skip"})
    assert "a@x.com" not in r2.text
    assert "dead@gone.com" in r2.text  # explicit skip export reveals junk


def test_lead_summary_carries_phone_field(tmp_path, monkeypatch):
    """The list contract exposes an EMPTY phone field (future-ready), so the
    Contacts screen can render a Phone column honestly until the pipeline
    starts collecting numbers."""
    client = _setup(tmp_path, monkeypatch)
    leads_module._store.save(_dossier("a@x.com", "x.com"))
    r = client.get("/api/v1/leads")
    assert r.json()[0]["phone"] == ""


# ---------------------------------------------------------------------------
# Data management (user-controlled delete / clear-junk)
# ---------------------------------------------------------------------------

def test_delete_lead_removes_dossier_and_pending(tmp_path, monkeypatch):
    """A dismissed lead is gone from both the dossiers store AND the discovery
    cache, so a future run never re-discovers / re-burns credits on it."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    pending = PendingLeadsStore(db_path=store._db_path)
    store.save(_dossier("a@x.com", "x.com"))
    pending.add([{"email": "a@x.com", "domain": "x.com", "company": "Acme", "location": "TX"}])
    assert pending.count() == 1

    r = client.delete("/api/v1/leads/a@x.com")
    assert r.status_code == 200
    assert r.json() == {"email": "a@x.com", "deleted": True}

    # dossier gone -> GET 404; pending also cleared.
    assert client.get("/api/v1/leads/a@x.com").status_code == 404
    assert pending.count() == 0

    # deleting a missing lead -> 404
    assert client.delete("/api/v1/leads/nope@x.com").status_code == 404


def test_delete_lead_reason_feeds_learning_with_user_identity(tmp_path, monkeypatch):
    """The delete dialog's structured reason reaches fit-learning WITH the
    caller's user id: ``not_our_client`` records a rejection attributed to this
    user, but a LONE uncorroborated verdict (the fake dossier's research liked
    the company) does not purge the identity — the 2026-09-12 gaming guard."""
    from app.lead_research.fit_learning import KIND_DOMAIN, FitLearningStore

    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com"))

    r = client.delete("/api/v1/leads/a@x.com?reason=not_our_client")
    assert r.status_code == 200

    learning = FitLearningStore(store._db_path)
    row = learning.get(KIND_DOMAIN, "x.com")
    assert row is not None and row["user_rejects"] == 1
    assert row["rejector_ids"] != ""  # attributed to the calling user
    assert row["corroborated"] == 0  # research did not agree
    assert learning.should_skip_domain("x.com") is False  # guard: not decisive


def test_delete_lead_admin_reason_is_corroborated(tmp_path, monkeypatch):
    """An ADMIN's not-our-client delete is decisive immediately — the operator
    is trusted (corroborated=1)."""
    from app.lead_research.fit_learning import KIND_DOMAIN, FitLearningStore

    client = _setup(tmp_path, monkeypatch, admin=True)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com"))

    r = client.delete("/api/v1/leads/a@x.com?reason=not_our_client")
    assert r.status_code == 200

    learning = FitLearningStore(store._db_path)
    row = learning.get(KIND_DOMAIN, "x.com")
    assert row is not None and row["corroborated"] == 1
    assert learning.should_skip_domain("x.com") is True


def test_clear_junk_removes_only_hidden_skip(tmp_path, monkeypatch):
    """clear-junk purges every re-gated skip dossier (junk: dead domain, low
    score, generic mail) but never touches actionable/nurture leads."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com", score=8.0, rec="contact_now"))
    store.save(_dossier("b@y.com", "y.com", score=4.0, rec="nurture", bound=False, person="Bo"))
    store.save(_dossier("junk@z.com", "z.com", score=2.0, rec="skip", bound=False))
    store.save(_dead_domain("dead@gone.com", "gone.com"))

    r = client.post("/api/v1/leads/clear-junk")
    assert r.status_code == 200
    body = r.json()
    assert body["removed"] == 2, body
    assert set(body["emails"]) == {"junk@z.com", "dead@gone.com"}

    # Only actionable + nurture remain.
    leads = client.get("/api/v1/leads").json()
    assert {l["email"] for l in leads} == {"a@x.com", "b@y.com"}


# ---------------------------------------------------------------------------
# Auth (M12 baseline)
# ---------------------------------------------------------------------------

def test_api_key_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(leads_module.settings, "LEADS_API_KEY", "sekret")
    client = _setup(tmp_path, monkeypatch)

    # no key -> 401
    assert client.get("/api/v1/leads").status_code == 401
    assert client.post("/api/v1/leads/jobs", json={
        "trade": "gc", "location": "TX",
    }).status_code == 401

    # wrong key -> 401
    assert client.get("/api/v1/leads", headers={"X-API-Key": "nope"}).status_code == 401

    # right key -> 200
    assert client.get("/api/v1/leads", headers={"X-API-Key": "sekret"}).status_code == 200


# ---------------------------------------------------------------------------
# Phase 2 — SQL paging / q search / tags / sources (server-side list)
# ---------------------------------------------------------------------------

def _force_created_at(store, email, ts):
    """Pin a dossier's created_at so date-sensitive SQL is deterministic."""
    import sqlite3
    from app.lead_research.service import _email_hash
    conn = sqlite3.connect(store._db_path)
    conn.execute("UPDATE dossiers SET created_at = ? WHERE email_hash = ?",
                 (ts, _email_hash(email)))
    conn.commit()
    conn.close()


def test_list_leads_paginates_and_reports_total(tmp_path, monkeypatch):
    """?limit&offset slice IN SQL (never the whole store); X-Total-Count reports
    the SAME filter's true total so the Companies screen can page honestly."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    for i, (email, domain) in enumerate(
            [("a@x.com", "x.com"), ("b@y.com", "y.com"),
             ("c@z.com", "z.com"), ("d@w.com", "w.com")], 1):
        store.save(_dossier(email, domain, score=float(8 - i)))

    r = client.get("/api/v1/leads", params={"limit": 2, "offset": 0})
    assert len(r.json()) == 2
    assert r.headers["X-Total-Count"] == "4"

    page2 = client.get("/api/v1/leads", params={"limit": 2, "offset": 2}).json()
    assert len(page2) == 2
    assert {l["email"] for l in r.json()} | {l["email"] for l in page2} == \
        {"a@x.com", "b@y.com", "c@z.com", "d@w.com"}

    # Beyond the end: empty page, honest total preserved.
    r = client.get("/api/v1/leads", params={"limit": 2, "offset": 4})
    assert r.json() == []
    assert r.headers["X-Total-Count"] == "4"


def test_list_leads_q_search(tmp_path, monkeypatch):
    """?q= is a literal, case-insensitive substring over email/domain/company/
    person/role — server-side, so paging a search never re-downloads."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@acme.com", "acme.com", company="Acme Corp"))
    store.save(_dossier("b@beta.com", "beta.com", person="Bob", company="Beta Ltd"))

    assert [l["email"] for l in client.get("/api/v1/leads", params={"q": "acme"}).json()] == ["a@acme.com"]
    assert [l["email"] for l in client.get("/api/v1/leads", params={"q": "Acme"}).json()] == ["a@acme.com"]
    assert [l["email"] for l in client.get("/api/v1/leads", params={"q": "bob"}).json()] == ["b@beta.com"]
    # literal-only: a % in the query is a literal %, never a wildcard.
    assert client.get("/api/v1/leads", params={"q": "nope"}).json() == []
    assert client.get("/api/v1/leads", params={"q": "%"}).json() == []


def test_list_tags_and_sources_endpoints(tmp_path, monkeypatch):
    """Global tag counts (one GROUP BY, skip+hidden excluded) + the source
    dropdown labels derived from query-run history."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com"))
    store.set_meta("a@x.com", tags=["Texas", "GC"])
    store.save(_dossier("b@y.com", "y.com"))
    store.set_meta("b@y.com", tags=["Texas"])
    store.save(_dossier("junk@z.com", "z.com", score=1.0))  # skipped
    store.set_meta("junk@z.com", tags=["Texas"])

    # The skip dossier's tag must NOT inflate the chip bar.
    by_tag = {t["tag"]: t["count"] for t in client.get("/api/v1/leads/tags").json()}
    assert by_tag == {"Texas": 2, "GC": 1}

    # Sources come from the query-run history (email -> "trade · location").
    leads_module._manager._store.save(Job(
        id="run-1",
        query={"trade": "General Contractors", "location": "Dallas TX", "target_emails": 2},
        state=JobState.completed,
        results=[{"email": "a@x.com"}, {"email": "b@y.com"}],
        created_at="2026-09-05T10:00:00",
        updated_at="2026-09-05T10:02:00",
    ))
    assert client.get("/api/v1/leads/sources").json() == ["General Contractors · Dallas TX"]


# ---------------------------------------------------------------------------
# Phase 2 — store-level: query_leads / backfill / dates / tag counts
# ---------------------------------------------------------------------------

def test_store_query_leads_filters_paging_total(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.save(_dossier("a@x.com", "x.com", score=8.0))
    store.save(_dossier("b@y.com", "y.com", score=6.0, rec="nurture"))
    store.save(_dossier("junk@z.com", "z.com", score=1.0, rec="skip"))
    store.set_meta("a@x.com", folder="Hot", tags=["TX"])
    store.set_meta("b@y.com", tags=["TX"])

    # Default (folder=None, no date/tag): the UNFILED inbox only — a filed lead
    # leaves the default view (the mailbox MOVE model).
    page, total = store.query_leads()
    assert [r["email"] for r in page] == ["b@y.com"]
    assert total == 1

    # folder="*" = every place: tier -> score ordering.
    page, total = store.query_leads(folder="*")
    assert [r["email"] for r in page] == ["a@x.com", "b@y.com"]
    assert total == 2

    page, total = store.query_leads(folder="*", min_score=7.0)
    assert [r["email"] for r in page] == ["a@x.com"]
    assert total == 1

    page, total = store.query_leads(folder="*", bound=True)
    assert total == 2

    page, total = store.query_leads(folder="Hot")
    assert [r["email"] for r in page] == ["a@x.com"]

    # A tag/date recall is GLOBAL (no place scoping) — the router passes
    # global_scope=True; filed + inbox leads both match.
    page, total = store.query_leads(tag="TX", global_scope=True)
    assert total == 2

    # Paging honours the same total for the SAME filter.
    page, total = store.query_leads(folder="*", limit=1, offset=0)
    assert len(page) == 1 and total == 2
    page, total = store.query_leads(folder="*", limit=1, offset=1)
    assert len(page) == 1 and total == 2
    page, total = store.query_leads(folder="*", limit=1, offset=2)
    assert page == [] and total == 2


def test_store_backfill_writes_filter_columns(tmp_path):
    """A pre-Phase-2 row (recommendation='') is re-gated on first open of a NEW
    process so SQL filtering sees the same verdicts as the Python reader."""
    db = str(tmp_path / "db.sqlite")
    store = LeadResearchStore(db_path=db)
    store.save(_dossier("a@x.com", "x.com"))

    # Simulate a legacy row: the columns exist but were never populated.
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("UPDATE dossiers SET recommendation='', potential_score=0, bound=0")
    conn.commit()
    conn.close()

    # A NEW process opens the DB; the guarded backfill re-gates the row.
    store2 = LeadResearchStore(db_path=db)
    page, total = store2.query_leads()
    assert total == 1
    row = page[0]
    assert row["recommendation"] == "contact_now"
    assert row["potential_score"] == 8.0
    assert row["bound"] == 1

    # Backfill is idempotent — re-opening doesn't re-write or error.
    store3 = LeadResearchStore(db_path=db)
    assert store3.query_leads()[1] == 1


def test_store_distinct_dates_and_tag_counts(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.save(_dossier("a@x.com", "x.com"))
    _force_created_at(store, "a@x.com", "2026-09-01 10:00:00")
    store.save(_dossier("b@y.com", "y.com"))
    _force_created_at(store, "b@y.com", "2026-09-02 10:00:00")
    store.save(_dossier("junk@z.com", "z.com", score=1.0, rec="skip"))
    _force_created_at(store, "junk@z.com", "2026-09-02 11:00:00")
    store.set_hidden("b@y.com", True)

    # Hidden dossiers' days drop out of recall; skip days stay (a harvest date
    # is a recall dimension, not a quality gate).
    assert store.distinct_dates() == ["2026-09-02", "2026-09-01"]

    # tag_counts: skip + hidden tags must not inflate the chip bar.
    store.set_meta("a@x.com", tags=["TX"])
    store.set_meta("junk@z.com", tags=["TX"])
    by_tag = {t["tag"]: t["count"] for t in store.tag_counts()}
    assert by_tag == {"TX": 1}
