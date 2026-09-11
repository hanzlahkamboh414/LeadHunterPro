"""Phase B — folders + tags organization.

Covers the user-metadata layer: the additive DB migration, the store's
set/get/sweep methods, and the HTTP contract (PUT /leads/{email}/organize,
POST /leads/organize/{rename,clear}, plus folder/tag filters on the list).

Kernel guarantee: user metadata lives OUTSIDE the research payload
(dossier_json), so a pipeline re-save never clobbers the user's folders/tags.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

import app.api.v1.leads as leads_module
from app.api.v1.leads import LeadResearchStore
from app.lead_research.models import CompanyProfile, LeadDossier, LeadMeta, PersonFindings
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
    """Point the router's store at a tmp DB (mirrors test_leads_api._setup).

    The store's ``save()`` is wrapped so seeded dossiers carry
    ``user_id=testuser.id`` — under strict isolation the test user only sees
    rows belonging to them.
    """
    from app.auth.jwt import create_access_token
    from app.auth.models import UserStore
    import app.auth.dependencies as deps
    # Create the test user FIRST so we know the user_id for store seeds.
    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    user = user_store.create("testuser", "test@example.com", "password")
    # Wrap the store so save() auto-injects user_id for seeded dossiers.
    _real_store = LeadResearchStore(db_path=str(tmp_path / "jobs.db"))
    _orig_save = _real_store.save
    _real_store.save = lambda d, **kw: _orig_save(d, user_id=kw.pop("user_id", "") or user.id)
    monkeypatch.setattr(leads_module, "_store", _real_store)
    token = create_access_token(user.id, user.is_admin, username=user.username)
    return TestClient(
        app,
        headers={"Authorization": f"Bearer {token}"},
    )


# ---------------------------------------------------------------------------
# Store-level: migration + meta methods
# ---------------------------------------------------------------------------

def test_init_db_migrates_old_schema(tmp_path):
    """A DB created BEFORE Phase B gains folder+tags columns on first open —
    additive, existing rows keep their dossier_json untouched."""
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
    assert store.get("a@x.com") is not None  # data survived

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dossiers)")}
    conn.close()
    assert "folder" in cols and "tags" in cols
    # Old row migrated cleanly: empty folder + empty tags, json untouched.
    m = store.get_meta("a@x.com")
    assert m == LeadMeta(folder="", tags=[])


def test_set_meta_roundtrip_and_missing_email(tmp_path, monkeypatch):
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.save(_dossier("a@x.com", "x.com"))

    assert store.set_meta("a@x.com", folder="Hot", tags=["TX", " tx ", "", "Hot"]) is True
    m = store.get_meta("a@x.com")
    assert m is not None
    assert m.folder == "Hot"
    # tags trimmed, empty dropped, exact duplicates removed, order preserved
    # (dedupe is case-sensitive by design — "TX" and "tx" are distinct tags).
    assert m.tags == ["TX", "tx", "Hot"]

    assert store.set_meta("nope@x.com", folder="Hot") is False
    assert store.get_meta("nope@x.com") is None


def test_save_never_clobbers_user_meta(tmp_path, monkeypatch):
    """THE core guarantee: re-saving a researched dossier (pipeline) leaves the
    user's folder/tags on the row even though dossier_json is rewritten."""
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    d = _dossier("a@x.com", "x.com", score=9.0)
    store.save(d)
    store.set_meta("a@x.com", folder="Hot", tags=["Urgent"])

    # Simulate a fresh research pass writing the SAME email again.
    store.save(_dossier("a@x.com", "x.com", score=7.0, person="Ji"))

    m = store.get_meta("a@x.com")
    assert m == LeadMeta(folder="Hot", tags=["Urgent"])
    refreshed = store.get("a@x.com")
    assert refreshed.potential_score == 7.0  # research data updated, meta kept


def test_all_meta_maps_every_dossier(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.save(_dossier("a@x.com", "x.com"))
    store.save(_dossier("b@y.com", "y.com"))
    store.set_meta("b@y.com", folder="TX")

    meta = store.all_meta()
    assert set(meta) == {_email_hash("a@x.com"), _email_hash("b@y.com")}
    assert meta[_email_hash("b@y.com")].folder == "TX"
    assert meta[_email_hash("a@x.com")].folder == ""


def test_rename_and_clear_sweeps(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.save(_dossier("a@x.com", "x.com"))
    store.save(_dossier("b@y.com", "y.com"))
    store.save(_dossier("c@z.com", "z.com"))
    store.set_meta("a@x.com", folder="Hot", tags=["TX", "Urgent"])
    store.set_meta("b@y.com", folder="Hot", tags=["TX"])
    store.set_meta("c@z.com", folder="Cold", tags=["Urgent"])

    # Rename folder Hot -> Priority (2 dossiers); tags kept.
    assert store.rename_folder("Hot", "Priority") == 2
    assert store.get_meta("a@x.com").folder == "Priority"
    assert store.get_meta("a@x.com").tags == ["TX", "Urgent"]
    assert store.get_meta("c@z.com").folder == "Cold"  # untouched

    # Rename a tag across dossiers (2 carry TX).
    assert store.rename_tag("TX", "Texas") == 2
    assert store.get_meta("a@x.com").tags == ["Texas", "Urgent"]
    assert store.get_meta("c@z.com").tags == ["Urgent"]

    # Clear a folder (1) + clear a tag (2).
    assert store.clear_folder("Cold") == 1
    assert store.get_meta("c@z.com").folder == ""
    assert store.clear_tag("Urgent") == 2
    assert store.get_meta("a@x.com").tags == ["Texas"]

    # No-op guards.
    assert store.rename_folder("", "X") == 0
    assert store.rename_folder("Same", "Same") == 0
    assert store.clear_tag("") == 0


# ---------------------------------------------------------------------------
# HTTP: organize endpoints + filters
# ---------------------------------------------------------------------------

def test_organize_endpoint_sets_and_echoes(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    leads_module._store.save(_dossier("a@x.com", "x.com"))

    r = client.put("/api/v1/leads/a@x.com/organize",
                   json={"folder": "Hot", "tags": ["TX", " Urgent "]})
    assert r.status_code == 200
    row = r.json()
    assert row["folder"] == "Hot"
    assert row["tags"] == ["TX", "Urgent"]  # server-side normalized
    assert row["email"] == "a@x.com"

    # List + detail both echo it. a@x.com was moved into a folder, so it lives
    # under ?folder=Hot (mailbox default view = the unfiled inbox).
    lead = client.get("/api/v1/leads", params={"folder": "Hot"}).json()[0]
    assert lead["folder"] == "Hot" and lead["tags"] == ["TX", "Urgent"]
    detail = client.get("/api/v1/leads/a@x.com").json()
    assert detail["folder"] == "Hot" and detail["tags"] == ["TX", "Urgent"]

    # 404 on a missing dossier.
    assert client.put("/api/v1/leads/nope@x.com/organize",
                      json={"folder": "", "tags": []}).status_code == 404


def test_list_leads_filters_by_folder_and_tag(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    # Distinct scores on a/b so the folder + tag ORDER is deterministic:
    # the sort key is (tier, -score), and the store's updated_at is only
    # SECOND-granularity — two equal-score leads saved in the same second
    # have no stable tie-break, so a test must not lean on it.
    store.save(_dossier("a@x.com", "x.com"))
    store.save(_dossier("b@y.com", "y.com", score=7.5))
    store.save(_dossier("c@z.com", "z.com", score=4.0, rec="nurture"))
    store.set_meta("a@x.com", folder="Hot", tags=["TX"])
    store.set_meta("b@y.com", folder="Hot", tags=["Urgent"])
    store.set_meta("c@z.com", folder="Cold", tags=["TX"])

    hot = client.get("/api/v1/leads", params={"folder": "Hot"}).json()
    # Actionable first, then score high-first: a (contact_now @ 8.0) before
    # b (contact_now @ 7.5). Deterministic, never a coin-flip page.
    assert [l["email"] for l in hot] == ["a@x.com", "b@y.com"]

    tx = client.get("/api/v1/leads", params={"tag": "TX"}).json()
    assert [l["email"] for l in tx] == ["a@x.com", "c@z.com"]

    combo = client.get("/api/v1/leads", params={"folder": "Hot", "tag": "TX"}).json()
    assert [l["email"] for l in combo] == ["a@x.com"]

    # Ordering intact: contact_now (a) before nurture (c) in the tag view.
    assert [l["email"] for l in tx] == ["a@x.com", "c@z.com"]


def test_organize_rename_and_clear_http(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com"))
    store.save(_dossier("b@y.com", "y.com"))
    store.set_meta("a@x.com", folder="Hot", tags=["TX"])
    store.set_meta("b@y.com", folder="Hot", tags=["TX"])

    # Rename folder + tag.
    r = client.post("/api/v1/leads/organize/rename",
                    json={"kind": "folder", "from_": "Hot", "to": "Priority"})
    assert r.status_code == 200 and r.json()["updated"] == 2
    r = client.post("/api/v1/leads/organize/rename",
                    json={"kind": "tag", "from_": "TX", "to": "Texas"})
    assert r.status_code == 200 and r.json()["updated"] == 2
    assert client.get("/api/v1/leads", params={"folder": "Priority"}).json()[0]["folder"] == "Priority"

    # Clear tag + folder.
    r = client.post("/api/v1/leads/organize/clear",
                    json={"kind": "tag", "value": "Texas"})
    assert r.status_code == 200 and r.json()["updated"] == 2
    r = client.post("/api/v1/leads/organize/clear",
                    json={"kind": "folder", "value": "Priority"})
    assert r.status_code == 200 and r.json()["updated"] == 2
    lead = client.get("/api/v1/leads").json()[0]
    assert lead["folder"] == "" and lead["tags"] == []

    # Bad kind -> 422.
    assert client.post("/api/v1/leads/organize/rename",
                       json={"kind": "nope", "from_": "x", "to": "y"}).status_code == 422
    assert client.post("/api/v1/leads/organize/clear",
                       json={"kind": "nope", "value": "x"}).status_code == 422


def test_delete_lead_removes_meta_with_row(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com"))
    store.set_meta("a@x.com", folder="Hot", tags=["TX"])

    assert client.delete("/api/v1/leads/a@x.com").status_code == 200
    assert client.get("/api/v1/leads/a@x.com").status_code == 404
    # Row is gone -> meta is gone with it (no orphan metadata).
    assert store.get_meta("a@x.com") is None


def test_old_json_survives_organize(tmp_path, monkeypatch):
    """organize writes ONLY folder/tags columns — dossier_json stays byte-same."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com"))
    conn = sqlite3.connect(store._db_path)
    before = conn.execute(
        "SELECT dossier_json FROM dossiers WHERE email=?", ("a@x.com",)
    ).fetchone()[0]
    conn.close()

    client.put("/api/v1/leads/a@x.com/organize", json={"folder": "Hot", "tags": ["TX"]})

    conn = sqlite3.connect(store._db_path)
    after = conn.execute(
        "SELECT dossier_json FROM dossiers WHERE email=?", ("a@x.com",)
    ).fetchone()[0]
    conn.close()
    assert after == before
    assert json.loads(after)["email"] == "a@x.com"