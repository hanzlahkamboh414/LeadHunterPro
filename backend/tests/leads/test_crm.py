"""Phase E1 — the CRM pipeline layer.

Covers: the additive DB migration (crm columns + crm_events table), the
store's set_crm/get_crm methods (kernel guarantee: stage + next action live
OUTSIDE dossier_json, so a pipeline re-save and organize never clobber them),
the HTTP contract (PUT /leads/{email}/crm, GET /leads/{email}/crm, the
crm_status list filter), honest defaults ('researched' — a stored dossier has
by definition been researched), event hygiene (no-op calls write no events,
delete purges the timeline), and per-user isolation.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

import app.api.v1.leads as leads_module
from app.api.v1.leads import LeadResearchStore
from app.auth.activity import ActivityStore
import app.auth.activity as activity_module
from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.lead_research.service import _email_hash
from app.main import app


def _dossier(email: str, domain: str, score: float = 8.0,
             rec: str = "contact_now", person: str = "Jane") -> LeadDossier:
    return LeadDossier(
        email=email,
        domain=domain,
        company=CompanyProfile(name="Acme", industry="general contractor"),
        person=PersonFindings(name=person, role="Owner", bound=True, role_relevance=True),
        potential_score=score,
        recommendation=rec,
    )


def _setup(tmp_path, monkeypatch):
    """Point the router's store + activity log at tmp DBs (test_leads_api
    pattern). Seeded dossiers carry user_id=testuser.id so strict isolation
    holds."""
    from app.auth.jwt import create_access_token
    from app.auth.models import UserStore
    import app.auth.dependencies as deps

    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    user = user_store.create("testuser", "test@example.com", "password")
    # update_crm records a "crm" activity row — keep it out of the real DB.
    monkeypatch.setattr(
        activity_module, "_activity_store",
        ActivityStore(db_path=str(tmp_path / "users.db")),
    )
    _real_store = LeadResearchStore(db_path=str(tmp_path / "jobs.db"))
    _orig_save = _real_store.save
    _real_store.save = lambda d, **kw: _orig_save(d, user_id=kw.pop("user_id", "") or user.id)
    monkeypatch.setattr(leads_module, "_store", _real_store)
    token = create_access_token(user.id, user.is_admin, username=user.username)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"}), _real_store


# ---------------------------------------------------------------------------
# Store-level: migration + defaults
# ---------------------------------------------------------------------------

def test_init_db_migrates_old_schema(tmp_path):
    """A DB created BEFORE Phase E1 gains crm_status/next_action + the
    crm_events table on first open — additive, dossier_json untouched."""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE dossiers (
            email_hash TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            domain TEXT NOT NULL,
            dossier_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO dossiers (email_hash, email, domain, dossier_json) VALUES (?,?,?,?)",
        (_email_hash("a@x.com"), "a@x.com", "x.com", '{"email":"a@x.com"}'),
    )
    conn.commit()
    conn.close()

    store = LeadResearchStore(db_path=db)
    crm = store.get_crm("a@x.com")
    # A stored dossier has BY DEFINITION been researched — the honest default.
    assert crm is not None
    assert crm["crm_status"] == "researched"
    assert crm["next_action"] == ""
    assert crm["events"] == []

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dossiers)")}
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "crm_status" in cols and "next_action" in cols
    assert "crm_events" in tables


def test_fresh_save_defaults_to_researched(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "s.db"))
    store.save(_dossier("b@x.com", "x.com"))
    crm = store.get_crm("b@x.com")
    assert crm["crm_status"] == "researched"
    assert crm["events"] == []


def test_set_crm_status_next_action_note(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "s.db"))
    store.save(_dossier("c@x.com", "x.com"))

    out = store.set_crm("c@x.com", status="contacted",
                        next_action="Send intro email",
                        note="Spoke to their PM at the expo",
                        user_id="u1", username="shakir")
    assert out == {"crm_status": "contacted", "next_action": "Send intro email"}

    crm = store.get_crm("c@x.com")
    assert crm["crm_status"] == "contacted"
    assert crm["next_action"] == "Send intro email"
    kinds = [e["kind"] for e in crm["events"]]
    assert kinds == ["status", "next_action", "note"]
    assert crm["events"][0]["detail"] == "researched → contacted"
    assert crm["events"][0]["username"] == "shakir"
    assert crm["events"][2]["detail"] == "Spoke to their PM at the expo"


def test_set_crm_noop_writes_no_event(tmp_path):
    """A call that changes NOTHING (same stage, empty note) is a no-op — the
    timeline only ever records real transitions, never fake events."""
    store = LeadResearchStore(db_path=str(tmp_path / "s.db"))
    store.save(_dossier("d@x.com", "x.com"))
    store.set_crm("d@x.com", status="contacted")
    out = store.set_crm("d@x.com", status="contacted")  # same stage again
    assert out["crm_status"] == "contacted"
    assert len(store.get_crm("d@x.com")["events"]) == 1


def test_set_crm_clears_next_action(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "s.db"))
    store.save(_dossier("e@x.com", "x.com"))
    store.set_crm("e@x.com", next_action="Call them")
    out = store.set_crm("e@x.com", next_action="   ")
    assert out["next_action"] == ""
    kinds = [e["kind"] for e in store.get_crm("e@x.com")["events"]]
    assert kinds == ["next_action", "next_action"]
    assert store.get_crm("e@x.com")["events"][1]["detail"] == "(cleared)"


def test_set_crm_missing_email_is_none(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "s.db"))
    assert store.set_crm("nobody@x.com", status="won") is None
    assert store.get_crm("nobody@x.com") is None


def test_crm_state_survives_resave_and_organize(tmp_path):
    """Kernel guarantee: stage + next action live outside dossier_json, so a
    pipeline re-save AND an organize (folder/tags) never clobber them."""
    store = LeadResearchStore(db_path=str(tmp_path / "s.db"))
    store.save(_dossier("f@x.com", "x.com"))
    store.set_crm("f@x.com", status="replied", next_action="Reply to John")

    store.save(_dossier("f@x.com", "x.com", score=9.5))  # re-research
    store.set_meta("f@x.com", folder="Hot leads", tags=["texas"])

    crm = store.get_crm("f@x.com")
    assert crm["crm_status"] == "replied"
    assert crm["next_action"] == "Reply to John"


def test_delete_purges_timeline(tmp_path):
    """A deleted lead leaves no orphan events behind; a restored lead
    honestly restarts at 'researched'."""
    store = LeadResearchStore(db_path=str(tmp_path / "s.db"))
    store.save(_dossier("g@x.com", "x.com"))
    store.set_crm("g@x.com", status="interested", note="wants a call")
    store.delete("g@x.com")

    conn = sqlite3.connect(str(tmp_path / "s.db"))
    n = conn.execute(
        "SELECT COUNT(*) FROM crm_events WHERE email_hash = ?",
        (_email_hash("g@x.com"),),
    ).fetchone()[0]
    conn.close()
    assert n == 0


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------

def test_put_crm_updates_stage(tmp_path, monkeypatch):
    client, store = _setup(tmp_path, monkeypatch)
    store.save(_dossier("h@x.com", "x.com"))

    r = client.put("/api/v1/leads/h@x.com/crm", json={"status": "contacted"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["crm_status"] == "contacted"
    assert [e["kind"] for e in body["events"]] == ["status"]
    assert body["events"][0]["detail"] == "researched → contacted"
    assert body["events"][0]["username"] == "testuser"

    # The stage rides on the DETAIL view too.
    r = client.get("/api/v1/leads/h@x.com")
    assert r.status_code == 200
    assert r.json()["crm_status"] == "contacted"


def test_put_crm_invalid_status_422(tmp_path, monkeypatch):
    client, store = _setup(tmp_path, monkeypatch)
    store.save(_dossier("i@x.com", "x.com"))
    r = client.put("/api/v1/leads/i@x.com/crm", json={"status": "hot lead"})
    assert r.status_code == 422
    # Nothing changed.
    assert store.get_crm("i@x.com")["crm_status"] == "researched"


def test_put_crm_foreign_dossier_404(tmp_path, monkeypatch):
    """Per-user isolation: another user's dossier is invisible (404, not 403)."""
    client, store = _setup(tmp_path, monkeypatch)
    # Seed a dossier owned by SOMEONE ELSE (bypass the user-injecting wrapper).
    from app.lead_research.service import LeadResearchStore as LRS
    other = LRS(db_path=store._db_path)
    other.save(_dossier("other@y.com", "y.com"), user_id="someone-else")
    r = client.put("/api/v1/leads/other@y.com/crm", json={"status": "won"})
    assert r.status_code == 404


def test_put_crm_note_and_next_action(tmp_path, monkeypatch):
    client, store = _setup(tmp_path, monkeypatch)
    store.save(_dossier("j@x.com", "x.com"))
    r = client.put("/api/v1/leads/j@x.com/crm",
                   json={"next_action": "Follow up Monday", "note": "Left a voicemail"})
    assert r.status_code == 200
    body = r.json()
    assert body["next_action"] == "Follow up Monday"
    kinds = [e["kind"] for e in body["events"]]
    assert kinds == ["next_action", "note"]


def test_get_crm_endpoint(tmp_path, monkeypatch):
    client, store = _setup(tmp_path, monkeypatch)
    store.save(_dossier("k@x.com", "x.com"))
    store.set_crm("k@x.com", status="meeting", note="Site visit Friday")
    r = client.get("/api/v1/leads/k@x.com/crm")
    assert r.status_code == 200
    body = r.json()
    assert body["crm_status"] == "meeting"
    assert [e["kind"] for e in body["events"]] == ["status", "note"]


def test_list_filter_by_crm_status(tmp_path, monkeypatch):
    client, store = _setup(tmp_path, monkeypatch)
    store.save(_dossier("l1@x.com", "x.com"))
    store.save(_dossier("l2@x.com", "x.com"))
    store.save(_dossier("l3@x.com", "x.com"))
    store.set_crm("l1@x.com", status="contacted")
    store.set_crm("l2@x.com", status="contacted")
    store.set_crm("l3@x.com", status="lost")

    r = client.get("/api/v1/leads", params={"crm_status": "contacted"})
    assert r.status_code == 200
    emails = {row["email"] for row in r.json()}
    assert emails == {"l1@x.com", "l2@x.com"}
    # The stage rides on every list row.
    assert all(row["crm_status"] == "contacted" for row in r.json())

    # The honest default: a fresh dossier filters as 'researched'.
    r = client.get("/api/v1/leads", params={"crm_status": "researched"})
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/api/v1/leads", params={"crm_status": "lost"})
    assert {row["email"] for row in r.json()} == {"l3@x.com"}


def test_crm_activity_recorded(tmp_path, monkeypatch):
    """The admin activity feed sees the stage change (who moved what, where)."""
    client, store = _setup(tmp_path, monkeypatch)
    store.save(_dossier("m@x.com", "x.com"))
    client.put("/api/v1/leads/m@x.com/crm", json={"status": "contacted"})
    rows = activity_module._activity_store.list(limit=10)
    crm_rows = [r for r in rows if r["action"] == "crm"]
    assert crm_rows and "m@x.com" in crm_rows[0]["detail"]
    assert "contacted" in crm_rows[0]["detail"]
