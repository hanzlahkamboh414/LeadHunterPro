"""Admin dashboard data control — hide / show / delete by date·search·lead.

Pins the admin's ability to control which data the USER dashboard shows:
``hidden`` rides on the dossiers table (additive, reversible), the user-facing
lead list + date dropdown exclude hidden rows, the admin visibility view reports
per-date counts, and delete permanently removes a whole date/search/lead through
the existing delete + audit path. Admin views stay unfiltered (admin is truth).
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

import app.api.v1.admin as admin_module
import app.api.v1.leads as leads_module
from app.api.v1.leads import JobManager
from app.admin_read import AdminReadRepository
from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.lead_research.service import LeadResearchStore
from app.main import app


def _dossier(email: str, name: str) -> LeadDossier:
    return LeadDossier(
        email=email,
        domain=email.split("@")[1],
        company=CompanyProfile(
            name=name, industry="general contractor", location="TX"
        ),
        person=PersonFindings(
            name="Owner", role="Owner", bound=True, role_relevance=True
        ),
        sources_checked=["https://example.test"],
        recommendation="contact_now",
        potential_score=8.0,
    )


#: email -> fixed research date the dossier is pinned to (deterministic tests).
_DATES = {"a@co.test": "2026-09-07", "b@co.test": "2026-09-07", "c@co.test": "2026-09-08"}


def _store(db_path: str) -> LeadResearchStore:
    """A store with three contact_now dossiers pinned to known dates."""
    store = LeadResearchStore(db_path=db_path)
    for email in _DATES:
        store.save(_dossier(email, email.split("@")[0].upper()))
    conn = sqlite3.connect(db_path)
    for email, date in _DATES.items():
        conn.execute(
            "UPDATE dossiers SET created_at = ? WHERE email = ?",
            (f"{date} 10:00:00", email),
        )
    conn.commit()
    conn.close()
    return store


# ---------------------------------------------------------------------------
# Store level — the hidden flag itself.
# ---------------------------------------------------------------------------

def test_set_hidden_and_hashes(tmp_path):
    store = _store(str(tmp_path / "lead_research.db"))
    assert store.hidden_hashes() == set()
    assert store.set_hidden("a@co.test", True) is True
    assert len(store.hidden_hashes()) == 1
    assert store.set_hidden("a@co.test", False) is True
    assert store.hidden_hashes() == set()
    assert store.set_hidden("missing@x.test", True) is False  # no such dossier


def test_set_hidden_bulk_counts_only_changed(tmp_path):
    store = _store(str(tmp_path / "lead_research.db"))
    assert store.set_hidden_bulk(["a@co.test", "b@co.test", "missing@x.test"], True) == 2
    assert len(store.hidden_hashes()) == 2
    # Re-hiding already-hidden rows changes nothing — honest 0.
    assert store.set_hidden_bulk(["a@co.test"], True) == 0
    assert store.set_hidden_bulk([], True) == 0


def test_visibility_by_date_reports_totals_and_hidden(tmp_path):
    store = _store(str(tmp_path / "lead_research.db"))
    store.set_hidden("a@co.test", True)
    view = store.visibility_by_date()
    assert view["2026-09-07"] == {"total": 2, "hidden": 1}
    assert view["2026-09-08"] == {"total": 1, "hidden": 0}


def test_folder_catalog_excludes_hidden_leads(tmp_path):
    store = _store(str(tmp_path / "lead_research.db"))
    store.set_meta("a@co.test", folder="Monday data", tags=["x"])
    assert store.folder_catalog()["total"] == 3
    assert store.folder_catalog()["unfiled"] == 2
    store.set_hidden("a@co.test", True)
    assert store.folder_catalog()["total"] == 2  # hidden lead not counted
    assert store.folder_catalog()["folders"][0]["count"] == 0


# ---------------------------------------------------------------------------
# API level — user views filter hidden; admin controls it.
# ---------------------------------------------------------------------------

def _client(monkeypatch, tmp_path) -> TestClient:
    store = _store(str(tmp_path / "lead_research.db"))
    monkeypatch.setattr(leads_module, "_store", store)
    monkeypatch.setattr(admin_module, "_store", store)
    monkeypatch.setattr(
        admin_module, "_reader", AdminReadRepository(str(tmp_path / "lead_research.db"))
    )
    monkeypatch.setattr(
        leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db"))
    )
    # Create admin user + JWT so leads endpoints (which require auth) pass.
    from app.auth.jwt import create_access_token
    from app.auth.models import UserStore
    import app.auth.dependencies as deps

    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    user = user_store.ensure_admin()
    token = create_access_token(user.id, user.is_admin, username=user.username)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_hide_by_date_removes_from_user_views_but_not_admin(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)

    # Admin visibility sees all 3, none hidden.
    vis = client.get("/api/v1/admin/leads/visibility").json()
    assert vis["total"] == 3 and vis["hidden"] == 0
    assert {d["date"]: d["total"] for d in vis["by_date"]} == {
        "2026-09-08": 1, "2026-09-07": 2,
    }

    # Hide the whole 7th.
    res = client.post("/api/v1/admin/leads/hide",
                      json={"scope": "date", "value": "2026-09-07"}).json()
    assert res["affected"] == 2

    # User list now shows only the 8th; the 7th is gone entirely.
    emails = [l["email"] for l in client.get("/api/v1/leads").json()]
    assert emails == ["c@co.test"]
    assert client.get("/api/v1/leads/dates").json() == ["2026-09-08"]

    # Admin still sees the truth: all 3 rows counted, 2 hidden from the user.
    vis = client.get("/api/v1/admin/leads/visibility").json()
    assert vis["total"] == 3 and vis["hidden"] == 2
    # 09-07 row (the hidden one): 2 total, 2 hidden; 09-08: 1 total, 0 hidden.
    assert {d["date"]: (d["total"], d["hidden"]) for d in vis["by_date"]} == {
        "2026-09-08": (1, 0), "2026-09-07": (2, 2),
    }


def test_show_restores_and_email_scope_is_reversible(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    client.post("/api/v1/admin/leads/hide", json={"scope": "email", "value": "b@co.test"})
    assert sorted(l["email"] for l in client.get("/api/v1/leads").json()) == [
        "a@co.test", "c@co.test",
    ]

    res = client.post("/api/v1/admin/leads/show", json={"scope": "email", "value": "b@co.test"}).json()
    assert res["affected"] == 1
    assert sorted(l["email"] for l in client.get("/api/v1/leads").json()) == [
        "a@co.test", "b@co.test", "c@co.test",
    ]


def test_delete_by_date_permanently_removes(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    res = client.post("/api/v1/admin/leads/delete",
                      json={"scope": "date", "value": "2026-09-08"}).json()
    assert res["affected"] == 1 and res["emails"] == ["c@co.test"]

    # Gone from the user list…
    emails = [l["email"] for l in client.get("/api/v1/leads").json()]
    assert emails == ["a@co.test", "b@co.test"]
    # …and from the admin truth (the row is really gone, not just hidden).
    vis = client.get("/api/v1/admin/leads/visibility").json()
    assert vis["total"] == 2 and vis["hidden"] == 0
    deleted = client.get("/api/v1/admin/deleted").json()["deleted"]
    assert any(r["email"] == "c@co.test" and r["reason"] == "admin" for r in deleted)


def test_action_on_empty_scope_is_honest_zero(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    res = client.post("/api/v1/admin/leads/hide",
                      json={"scope": "date", "value": "1999-01-01"}).json()
    assert res["affected"] == 0 and res["emails"] == []