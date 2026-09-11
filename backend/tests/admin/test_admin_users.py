"""Admin user management + activity log + assign + search cap — the multi-user
batch. Covers the Sprint2.12 surface: accounts CRUD, per-user activity,
push-a-folder-to-a-user, the 150-target cap, and the own-data-only dashboard
rule (a user's searches never appear on the admin's own dashboard).
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

import app.api.v1.admin as admin_module
import app.api.v1.auth as auth_module
import app.api.v1.leads as leads_module
import app.auth.activity as activity_module
import app.auth.dependencies as deps
from app.api.v1.leads import JobManager, LeadResearchStore
from app.auth.activity import ActivityStore
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.main import app


def _dossier(email: str, domain: str, score: float = 8.0,
             rec: str = "contact_now") -> LeadDossier:
    return LeadDossier(
        email=email,
        domain=domain,
        company=CompanyProfile(name="Acme", industry="general contractor",
                               location="Texas"),
        person=PersonFindings(name="Jane", role="Owner", bound=True,
                              role_relevance=True),
        potential_score=score,
        recommendation=rec,
    )


def _wait_state(client, job_id, want, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = client.get(f"/api/v1/leads/jobs/{job_id}")
        if r.status_code == 200 and r.json()["state"] == want:
            return r.json()
        time.sleep(0.01)
    raise TimeoutError(f"job {job_id} did not reach {want}")


def _client_for(user) -> TestClient:
    token = create_access_token(user.id, user.is_admin, username=user.username)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _setup(tmp_path, monkeypatch, run_full=None):
    """tmp users.db (accounts + activity) + tmp lead store; returns everything
    the tests need: (admin_client, user_store, activity, admin)."""
    if run_full is not None:
        monkeypatch.setattr("app.leads.jobs.run_full", run_full)
    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    monkeypatch.setattr(auth_module, "_store", user_store)
    activity = ActivityStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(activity_module, "_activity_store", activity)
    store = LeadResearchStore(db_path=str(tmp_path / "leads.db"))
    monkeypatch.setattr(leads_module, "_store", store)
    # admin.py binds _store at import time (from app.api.v1.leads import _store)
    # so it needs its own patch — the assign/visibility endpoints read it there.
    monkeypatch.setattr(admin_module, "_store", store)
    monkeypatch.setattr(
        leads_module, "_manager", JobManager(db_path=str(tmp_path / "leads.db"))
    )
    admin = user_store.ensure_admin()
    return _client_for(admin), user_store, activity, admin, store


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

def test_admin_creates_lists_and_deletes_users(tmp_path, monkeypatch):
    admin_client, user_store, _, admin, _ = _setup(tmp_path, monkeypatch)

    r = admin_client.post("/api/v1/admin/users", json={
        "username": "fieldguy", "email": "field@example.com", "password": "secret1",
    })
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["username"] == "fieldguy"
    assert "password" not in created and "password_hash" not in created

    # Duplicate username -> 409 (UserStore.create raises ValueError).
    r = admin_client.post("/api/v1/admin/users", json={
        "username": "fieldguy", "email": "other@example.com", "password": "secret1",
    })
    assert r.status_code == 409

    # Listing shows admin + the new account, oldest first.
    r = admin_client.get("/api/v1/admin/users")
    assert r.status_code == 200
    listed = r.json()
    assert listed["total"] == 2
    assert [u["username"] for u in listed["users"]] == [admin.username, "fieldguy"]

    # Delete: unknown -> 404, self -> 422, real user -> gone.
    assert admin_client.delete("/api/v1/admin/users/nope").status_code == 404
    assert admin_client.delete(f"/api/v1/admin/users/{admin.id}").status_code == 422
    r = admin_client.delete(f"/api/v1/admin/users/{created['id']}")
    assert r.status_code == 200 and r.json()["username"] == "fieldguy"
    assert user_store.get_by_username("fieldguy") is None


def test_admin_resets_user_password(tmp_path, monkeypatch):
    admin_client, user_store, _, admin, _ = _setup(tmp_path, monkeypatch)
    created = admin_client.post("/api/v1/admin/users", json={
        "username": "fieldguy", "email": "field@example.com", "password": "oldpass",
    }).json()

    r = admin_client.post(
        f"/api/v1/admin/users/{created['id']}/password",
        json={"new_password": "brandnew"},
    )
    assert r.status_code == 200

    # Old password no longer works; the new one does (same users.db store).
    assert user_store.verify_password("fieldguy", "oldpass") is None
    assert user_store.verify_password("fieldguy", "brandnew") is not None

    # Unknown user -> 404.
    assert admin_client.post(
        "/api/v1/admin/users/nope/password", json={"new_password": "x1234"}
    ).status_code == 404


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

def test_activity_log_records_login_logout_signup_and_search(tmp_path, monkeypatch):
    admin_client, user_store, activity, admin, _ = _setup(tmp_path, monkeypatch)

    # A user signs up through the public endpoint (records "signup")…
    anon = TestClient(app)
    r = anon.post("/api/v1/auth/signup", json={
        "username": "fieldguy", "email": "field@example.com", "password": "secret1",
    })
    assert r.status_code == 201
    user_id = r.json()["user_id"]

    # …logs in and out (records "login" + "logout").
    assert anon.post("/api/v1/auth/login", json={
        "username": "fieldguy", "password": "secret1",
    }).status_code == 200
    user_client = _client_for(user_store.get_by_id(user_id))
    assert user_client.post("/api/v1/auth/logout").status_code == 200

    rows = activity.list()
    actions = {row["action"] for row in rows}
    assert {"signup", "login", "logout"} <= actions
    # Newest first.
    assert rows[0]["action"] == "logout"

    # The per-user filter narrows to just that account's history.
    mine = activity.list(user_id=user_id)
    assert mine and all(row["user_id"] == user_id for row in mine)

    # The admin endpoint serves the same rows over HTTP.
    r = admin_client.get("/api/v1/admin/activity")
    assert r.status_code == 200
    served = r.json()
    assert served["total"] == len(rows)
    assert served["activity"][0]["action"] == "logout"

    r = admin_client.get("/api/v1/admin/activity", params={"user_id": user_id})
    assert all(row["user_id"] == user_id for row in r.json()["activity"])


def test_search_submission_records_activity(tmp_path, monkeypatch):
    def fake(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        return {"working_leads": 0, "leads_found": 0, "shortfall": 0,
                "shortfall_reason": "test"}

    admin_client, _, activity, admin, _ = _setup(tmp_path, monkeypatch, run_full=fake)
    r = admin_client.post("/api/v1/leads/jobs", json={
        "trade": "Roofing", "location": "Houston, TX", "target_emails": 10,
    })
    assert r.status_code == 201
    _wait_state(admin_client, r.json()["id"], "completed")

    searches = [row for row in activity.list() if row["action"] == "search"]
    assert len(searches) == 1
    assert searches[0]["user_id"] == admin.id
    assert "Roofing" in searches[0]["detail"]
    assert "10 targets" in searches[0]["detail"]


# ---------------------------------------------------------------------------
# Search cap — 150 for users, 500 for admin
# ---------------------------------------------------------------------------

def test_search_cap_150_for_users_admin_allows_500(tmp_path, monkeypatch):
    def fake(query, emit=None, cancel=None, store=None, paused=None, user_id=""):
        return {"working_leads": 0, "leads_found": 0, "shortfall": 0,
                "shortfall_reason": "test"}

    admin_client, user_store, _, admin, _ = _setup(
        tmp_path, monkeypatch, run_full=fake
    )
    plain = user_store.create("plain", "plain@example.com", "password")
    plain_client = _client_for(plain)

    # Non-admin over the cap -> 422, job never created.
    r = plain_client.post("/api/v1/leads/jobs", json={
        "trade": "Roofing", "location": "Houston, TX", "target_emails": 200,
    })
    assert r.status_code == 422

    # Exactly 150 is allowed for a user…
    r = plain_client.post("/api/v1/leads/jobs", json={
        "trade": "Roofing", "location": "Houston, TX", "target_emails": 150,
    })
    assert r.status_code == 201

    # …and 500 is fine for the admin.
    r = admin_client.post("/api/v1/leads/jobs", json={
        "trade": "Roofing", "location": "Houston, TX", "target_emails": 500,
    })
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# Assign — push a folder to a user's dashboard
# ---------------------------------------------------------------------------

def test_assign_folder_pushes_leads_to_user_dashboard(tmp_path, monkeypatch):
    admin_client, user_store, _, admin, store = _setup(tmp_path, monkeypatch)
    user = user_store.create("fieldguy", "field@example.com", "password")
    user_client = _client_for(user)

    # Admin researches 2 leads and files one into "Q3 Outreach".
    store.save(_dossier("a@acme.test", "acme.test"), user_id=admin.id)
    store.save(_dossier("b@beta.test", "beta.test"), user_id=admin.id)
    store.set_meta("b@beta.test", folder="Q3 Outreach")

    # Before: the user's dashboard is completely empty — no unfiled leads, and
    # NOT even the admin's folder NAME (the catalog is per-account now: a
    # folder belongs to the account that created/uses it, exactly like data).
    assert user_client.get("/api/v1/leads").json() == []
    before = {f["name"]: f["count"]
              for f in user_client.get("/api/v1/leads/folders").json()["folders"]}
    assert before == {}

    # Push the folder — one click, one call.
    r = admin_client.post("/api/v1/admin/leads/assign", json={
        "scope": "folder", "value": "Q3 Outreach", "user_id": user.id,
    })
    assert r.status_code == 200
    assert r.json()["affected"] == 1

    # The folder now holds exactly its lead on the user's dashboard (the default
    # Companies view is the unfiled inbox; folders are the slices).
    folders = user_client.get("/api/v1/leads/folders").json()
    assert {f["name"]: f["count"] for f in folders["folders"]} == {"Q3 Outreach": 1}
    assert folders["total"] == 1
    emails = [l["email"] for l in user_client.get(
        "/api/v1/leads", params={"folder": "Q3 Outreach"}
    ).json()]
    assert emails == ["b@beta.test"]

    # The lead is now the USER's: it left the admin's OWN dashboard (the admin
    # panel still sees everything — that is the drill-down view, not this one).
    admin_emails = {l["email"] for l in admin_client.get("/api/v1/leads").json()}
    assert admin_emails == {"a@acme.test"}

    # Take back: unassign empties the user's dashboard again.
    r = admin_client.post("/api/v1/admin/leads/assign", json={
        "scope": "folder", "value": "Q3 Outreach", "user_id": "",
    })
    assert r.status_code == 200 and r.json()["affected"] == 1
    assert user_client.get("/api/v1/leads", params={"folder": "Q3 Outreach"}).json() == []


# ---------------------------------------------------------------------------
# Own-data-only dashboards — a user's searches never hit the admin's dashboard
# ---------------------------------------------------------------------------

def test_admin_dashboard_shows_own_and_legacy_not_other_users(tmp_path, monkeypatch):
    admin_client, user_store, _, admin, store = _setup(tmp_path, monkeypatch)
    user = user_store.create("fieldguy", "field@example.com", "password")
    user_client = _client_for(user)

    # Three kinds of rows: legacy (pre-auth, user_id=""), the admin's own,
    # and another user's search results.
    store.save(_dossier("legacy@old.test", "old.test"), user_id="")
    store.save(_dossier("mine@admin.test", "admin.test"), user_id=admin.id)
    store.save(_dossier("theirs@user.test", "user.test"), user_id=user.id)

    # Admin's OWN dashboard: legacy + own — NEVER the user's data.
    admin_emails = {l["email"] for l in admin_client.get("/api/v1/leads").json()}
    assert admin_emails == {"legacy@old.test", "mine@admin.test"}

    # The user's dashboard: strictly their own rows.
    user_emails = {l["email"] for l in user_client.get("/api/v1/leads").json()}
    assert user_emails == {"theirs@user.test"}

    # The admin PANEL (unfiltered) still sees everything — the drill-down view.
    r = admin_client.get("/api/v1/admin/users/{}/leads-summary".format(user.id))
    assert r.status_code == 200
    summary = r.json()
    assert summary["user_id"] == user.id
    assert summary["total"] == 1
