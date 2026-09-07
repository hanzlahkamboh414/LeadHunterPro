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

def _setup(tmp_path, monkeypatch, run_full=None):
    """Point the router's manager + store at a tmp DB; optionally fake the pipeline."""
    if run_full is not None:
        monkeypatch.setattr("app.leads.jobs.run_full", run_full)
    monkeypatch.setattr(
        leads_module, "_manager",
        JobManager(db_path=str(tmp_path / "jobs.db")),
    )
    monkeypatch.setattr(
        leads_module, "_store",
        LeadResearchStore(db_path=str(tmp_path / "jobs.db")),
    )
    return TestClient(app)


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
    def fake(query, emit=None, cancel=None, store=None, paused=None):
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


def test_validation_rejects_bad_query(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    r = client.post("/api/v1/leads/jobs", json={"trade": "", "location": "", "target_emails": 0})
    assert r.status_code == 422


def test_get_job_404(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    r = client.get("/api/v1/leads/jobs/nope")
    assert r.status_code == 404


def test_cancel_job(tmp_path, monkeypatch):
    def blocking(query, emit=None, cancel=None, store=None, paused=None):
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


def _pause_blocking(query, emit=None, cancel=None, store=None, paused=None):
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
    """Default view = ACTIONABLE ONLY, deterministically ordered: tier
    (contact_now → nurture) first, then score high→low. Dead-domain / skip
    dossiers are hidden (they never inflate the count — the user's complaint)
    and can't scramble the order. Even when a nurture row is NEWER and HIGHER
    scored, contact_now still wins the tier, so the page never 'manipulates'."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("high@x.com", "x.com", score=8.0, rec="contact_now"))
    store.save(_dossier("low@x.com", "x.com", score=6.0, rec="contact_now"))
    # nurture saved LAST = newest, and OUTSCORES both — but tier must beat it.
    store.save(_dossier("nurture@y.com", "y.com", score=8.5, rec="nurture", bound=False, person="Bo"))
    store.save(_dead_domain("dead@gone.com", "gone.com"))
    store.save(_dossier("junk@z.com", "z.com", score=2.0, rec="skip", bound=False))

    r = client.get("/api/v1/leads")
    assert r.status_code == 200
    emails = [l["email"] for l in r.json()]
    assert emails == ["high@x.com", "low@x.com", "nurture@y.com"], emails


def test_list_leads_skip_filter_reveals_junk_ordered_by_score(tmp_path, monkeypatch):
    """?recommendation=skip still reveals junk — score high-first, dead-domain
    last — for the user who explicitly wants to inspect it."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com", score=8.0, rec="contact_now"))
    store.save(_dead_domain("dead@gone.com", "gone.com"))
    store.save(_dossier("junk@z.com", "z.com", score=2.0, rec="skip", bound=False))

    r = client.get("/api/v1/leads", params={"recommendation": "skip"})
    assert [l["email"] for l in r.json()] == ["junk@z.com", "dead@gone.com"]


def test_leads_carry_source_run_and_filter(tmp_path, monkeypatch):
    """Each lead names the query run that produced it, and can be filtered by
    it — a fresh search's leads are separated from older runs (no more mix)."""
    def fake(query, emit=None, cancel=None, store=None, paused=None):
        store.save(_dossier("a@x.com", "x.com"))
        store.save(_dossier("b@y.com", "y.com", score=4.0, rec="nurture", bound=False, person="Bo"))
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
