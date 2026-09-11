"""Phase B.2 — first-class folder catalog + extraction-date (created_at).

Covers the persisted (possibly empty) folder groups — the user's "create a
folder like 'Monday data', click it, then move selected leads in" flow — plus
the date filter so the user can see which data was extracted on which day.

Kernel guarantees:
- A folder exists BEFORE any lead is in it (catalog row, not derived-only).
- The catalog self-heals names that only ever rode on leads (no data loss).
- Rename/clear keep catalog and dossiers in sync; deleting a folder releases
  its leads (honest count).
- ``created_at`` (the date a lead was first researched) is exposed on list +
  detail and filterable via ?date=YYYY-MM-DD.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

import app.api.v1.leads as leads_module
from app.api.v1.leads import LeadResearchStore
from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.lead_research.service import _email_hash
from app.main import app


def _dossier(email: str, domain: str, score: float = 8.0,
             rec: str = "contact_now", person: str = "Jane") -> LeadDossier:
    # location non-empty (R2): a contact_now dossier must have a verified
    # geolocation — same rule that demotes location-less dossiers to nurture.
    return LeadDossier(
        email=email,
        domain=domain,
        company=CompanyProfile(name="Acme", industry="general contractor", location="Texas"),
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
    client, uid = _authed_client(tmp_path, monkeypatch)
    _real_store = LeadResearchStore(db_path=str(tmp_path / "jobs.db"))
    _orig_save = _real_store.save
    _real_store.save = lambda d, **kw: _orig_save(d, user_id=kw.pop("user_id", "") or uid)
    monkeypatch.setattr(leads_module, "_store", _real_store)
    return client


def _authed_client(tmp_path, monkeypatch):
    """TestClient sending a real JWT (leads endpoints require auth now).

    Returns ``(client, user_id)`` so ``_setup`` can scope seeded dossiers to
    this user.
    """
    from app.auth.jwt import create_access_token
    from app.auth.models import UserStore
    import app.auth.dependencies as deps
    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    user = user_store.create("testuser", "test@example.com", "password")
    token = create_access_token(user.id, user.is_admin, username=user.username)
    return TestClient(
        app,
        headers={"Authorization": f"Bearer {token}"},
    ), user.id


def _force_created_at(store: LeadResearchStore, email: str, ts: str) -> None:
    """Pin a dossier's created_at so date filters are deterministic."""
    conn = sqlite3.connect(store._db_path)
    conn.execute(
        "UPDATE dossiers SET created_at = ? WHERE email_hash = ?",
        (ts, _email_hash(email)),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Store-level: catalog
# ---------------------------------------------------------------------------

def test_migration_old_db_gains_folders_table(tmp_path):
    """A DB created BEFORE Phase B.2 gains the folders table on first open."""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE dossiers (
            email_hash TEXT PRIMARY KEY, email TEXT NOT NULL, domain TEXT NOT NULL,
            dossier_json TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    store = LeadResearchStore(db_path=db)
    as_names = {r[0] for r in
                sqlite3.connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "folders" in as_names
    assert store.list_folders() == []  # empty catalog, no folders yet


def test_create_folder_persists_empty_and_idempotent(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    # Create BEFORE any lead exists — the whole point of "pehle folder banao".
    assert store.create_folder("Monday data") is True
    folders = store.list_folders()
    assert len(folders) == 1
    assert folders[0]["name"] == "Monday data"
    assert folders[0]["count"] == 0  # empty is a real, clickable folder
    assert folders[0]["created_at"]

    # Duplicate create is idempotent — the folder already exists.
    assert store.create_folder("Monday data") is False
    assert store.create_folder("   ") is False  # blank name rejected


def test_list_folders_self_heals_derived_names(tmp_path):
    """Folders that only ever rode on leads (legacy) appear in the catalog."""
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.create_folder("Empty group")  # deliberately empty
    store.save(_dossier("a@x.com", "x.com"))
    store.set_meta("a@x.com", folder="Hot", tags=["TX"])
    store.save(_dossier("b@y.com", "y.com"))
    store.set_meta("b@y.com", folder="Hot", tags=["TX"])

    by_name = {f["name"]: f["count"] for f in store.list_folders()}
    # "Hot" never had a catalog row — self-healed from the dossiers, counted 2.
    assert by_name == {"Empty group": 0, "Hot": 2}


def test_rename_folder_syncs_catalog_and_leads(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.create_folder("Hot")
    store.create_folder("OldEmpty")
    store.save(_dossier("a@x.com", "x.com"))
    store.set_meta("a@x.com", folder="Hot")
    store.save(_dossier("b@y.com", "y.com"))
    store.set_meta("b@y.com", folder="Hot")

    assert store.rename_folder("Hot", "Priority") == 2
    # leads moved
    assert store.get_meta("a@x.com").folder == "Priority"
    # catalog: old gone, new present with count
    by_name = {f["name"]: f["count"] for f in store.list_folders()}
    assert "Hot" not in by_name
    assert by_name["Priority"] == 2
    # an EMPTY renamed folder survives the rename as an empty group
    assert store.rename_folder("OldEmpty", "Archive") == 0
    by_name = {f["name"]: f["count"] for f in store.list_folders()}
    assert "Archive" in by_name and by_name["Archive"] == 0


def test_clear_folder_deletes_catalog_and_releases_leads(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.create_folder("Hot")
    store.save(_dossier("a@x.com", "x.com"))
    store.set_meta("a@x.com", folder="Hot")
    store.create_folder("Empty")

    # Releasing "Hot" returns 1 — the lead it held is released (folder="").
    assert store.clear_folder("Hot") == 1
    assert store.get_meta("a@x.com").folder == ""
    names = {f["name"] for f in store.list_folders()}
    assert "Hot" not in names
    assert "Empty" in names  # untouched group stays

    # Clearing an empty folder is a valid "delete the group" (0 released).
    assert store.clear_folder("Empty") == 0
    assert "Empty" not in {f["name"] for f in store.list_folders()}


# ---------------------------------------------------------------------------
# Store-level: extraction date (created_at)
# ---------------------------------------------------------------------------

def test_research_dates_map_every_dossier(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.save(_dossier("a@x.com", "x.com"))
    _force_created_at(store, "a@x.com", "2026-09-01 09:30:00")
    store.save(_dossier("b@y.com", "y.com"))
    _force_created_at(store, "b@y.com", "2026-09-04 11:00:00")

    dates = store.research_dates()
    assert dates[_email_hash("a@x.com")] == "2026-09-01"
    assert dates[_email_hash("b@y.com")] == "2026-09-04"


# ---------------------------------------------------------------------------
# HTTP: folders + date
# ---------------------------------------------------------------------------

def test_folders_http_create_list_and_422(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)

    r = client.post("/api/v1/leads/folders", json={"name": "Monday data"})
    assert r.status_code == 201
    row = r.json()
    assert row["name"] == "Monday data"
    assert row["created"] is True and row["count"] == 0

    r = client.post("/api/v1/leads/folders", json={"name": "Monday data"})
    assert r.status_code == 201
    assert r.json()["created"] is False  # idempotent duplicate

    assert client.post("/api/v1/leads/folders", json={"name": "   "}).status_code == 422

    body = client.get("/api/v1/leads/folders").json()
    folders = body["folders"]
    assert [f["name"] for f in folders] == ["Monday data"]
    assert folders[0]["count"] == 0
    # Mailbox overview: unfiled inbox + total, so the Companies rail + Dashboard
    # can show the "Unfiled (n)" / "All (n)" chips honestly.
    assert body["unfiled"] == 0
    assert body["total"] == 0


def test_list_leads_date_filter_and_created_at(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com"))
    store.save(_dossier("b@y.com", "y.com", score=6.0, rec="nurture"))
    _force_created_at(store, "a@x.com", "2026-09-01 10:00:00")
    _force_created_at(store, "b@y.com", "2026-09-04 10:00:00")

    # Summary rows carry created_at (YYYY-MM-DD).
    days = {l["email"]: l["created_at"] for l in client.get("/api/v1/leads").json()}
    assert days == {"a@x.com": "2026-09-01", "b@y.com": "2026-09-04"}

    # Date filter: only that day's leads, ordering intact.
    sep1 = client.get("/api/v1/leads", params={"date": "2026-09-01"}).json()
    assert [l["email"] for l in sep1] == ["a@x.com"]
    assert client.get("/api/v1/leads", params={"date": "2026-09-04"}).json()[0]["email"] == "b@y.com"
    assert client.get("/api/v1/leads", params={"date": "1999-01-01"}).json() == []

    # Detail exposes it too.
    view = client.get("/api/v1/leads/a@x.com").json()
    assert view["created_at"] == "2026-09-01"


def test_folder_filters_drive_http_and_echo_created_at(tmp_path, monkeypatch):
    """End to end: create folder -> move leads -> date+folder filters compose."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com"))
    store.save(_dossier("b@y.com", "y.com"))
    _force_created_at(store, "a@x.com", "2026-09-01 10:00:00")
    _force_created_at(store, "b@y.com", "2026-09-01 10:00:00")

    client.post("/api/v1/leads/folders", json={"name": "Monday data"})
    for email in ("a@x.com", "b@y.com"):
        r = client.put(f"/api/v1/leads/{email}/organize",
                       json={"folder": "Monday data", "tags": []})
        assert r.status_code == 200
        assert r.json()["created_at"] == "2026-09-01"  # organize echoes date

    # Pin updated_at EQUAL so the equal-score order is the deterministic saved
    # (rowid) order. Without this the two saves / two organizes can straddle a
    # second boundary (slow machines), flipping the updated_at-DESC tie and
    # making the folder-list order a timing flake.
    conn = sqlite3.connect(store._db_path)
    conn.execute("UPDATE dossiers SET updated_at = '2026-09-01 11:00:00' WHERE email_hash IN (?, ?)",
                 (_email_hash("a@x.com"), _email_hash("b@y.com")))
    conn.commit()
    conn.close()

    # Folder filter shows only the moved leads; combined with date it still does.
    moved = client.get("/api/v1/leads", params={"folder": "Monday data"}).json()
    assert [l["email"] for l in moved] == ["a@x.com", "b@y.com"]
    composed = client.get("/api/v1/leads", params={
        "folder": "Monday data", "date": "2026-09-01"}).json()
    assert [l["email"] for l in composed] == ["a@x.com", "b@y.com"]

    # Deleting the folder releases its leads (honest count) + drops the group.
    r = client.post("/api/v1/leads/organize/clear", json={"kind": "folder", "value": "Monday data"})
    assert r.status_code == 200 and r.json()["updated"] == 2
    assert client.get("/api/v1/leads/folders").json()["folders"] == []
    assert {l["email"] for l in client.get("/api/v1/leads").json()} == {"a@x.com", "b@y.com"}


# ---------------------------------------------------------------------------
# Mailbox model (Phase B.3) — the default Companies view is the UNFILED inbox.
# A lead moved into a folder LEAVES that default view; it lives inside the
# folder now. `folder="*"` = All places; date is the global recall dimension.
# ---------------------------------------------------------------------------

def test_default_view_is_unfiled_inbox(tmp_path, monkeypatch):
    """Moving a lead into a folder removes it from the default Companies list.

    This is the user's core complaint ("sirf lable hi laga, sara data phir b
    companies section ma show ho rha ha") — folderization must be a MOVE, not
    a label. Default (no folder) = inbox-only; `?folder=NAME` = that folder.
    """
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("inbox@x.com", "x.com"))
    store.save(_dossier("filed@y.com", "y.com"))
    store.set_meta("filed@y.com", folder="Monday data")

    inbox = {l["email"] for l in client.get("/api/v1/leads").json()}
    assert inbox == {"inbox@x.com"}  # "filed@y.com" no longer appears by default

    filed = {l["email"] for l in client.get("/api/v1/leads", params={"folder": "Monday data"}).json()}
    assert filed == {"filed@y.com"}


def test_folder_star_is_all_places(tmp_path, monkeypatch):
    """`folder="*"` shows EVERY dossier (inbox + all folders) — the Dashboard's
    overview links and the Contacts outreach list need the full universe."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("inbox@x.com", "x.com"))
    store.save(_dossier("filed@y.com", "y.com"))
    store.set_meta("filed@y.com", folder="Monday data")

    all_leads = {l["email"] for l in client.get("/api/v1/leads", params={"folder": "*"}).json()}
    assert all_leads == {"inbox@x.com", "filed@y.com"}


def test_date_search_is_global_across_folders(tmp_path, monkeypatch):
    """The date dropdown is the "kis tareekh ko kya nikla" recall tool — with no
    folder selected it searches EVERY dossier (folderized included), so a moved
    lead's extraction day stays findable. A folder + date composes per-place."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("inbox@x.com", "x.com"))
    _force_created_at(store, "inbox@x.com", "2026-09-01 10:00:00")
    store.save(_dossier("filed@y.com", "y.com"))
    _force_created_at(store, "filed@y.com", "2026-09-01 10:00:00")
    store.save(_dossier("other@z.com", "z.com"))
    _force_created_at(store, "other@z.com", "2026-09-04 10:00:00")
    store.set_meta("filed@y.com", folder="Monday data")

    # No folder -> the whole day's harvest is shown, folder or not.
    harvest = {l["email"] for l in client.get("/api/v1/leads", params={"date": "2026-09-01"}).json()}
    assert harvest == {"inbox@x.com", "filed@y.com"}

    # Folder composed -> scoped to that folder's leads on that day.
    in_folder = client.get("/api/v1/leads", params={
        "folder": "Monday data", "date": "2026-09-01"}).json()
    assert {l["email"] for l in in_folder} == {"filed@y.com"}

    # The dates endpoint lists EVERY extraction day, folders included.
    assert client.get("/api/v1/leads/dates").json() == ["2026-09-04", "2026-09-01"]


def test_folder_catalog_unfiled_and_total(tmp_path, monkeypatch):
    """`GET /leads/folders` reports unfiled (default view size) + total
    (everything) so the UI rail + Dashboard stay honest even after foldering."""
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("a@x.com", "x.com"))
    store.save(_dossier("filed@y.com", "y.com"))
    store.set_meta("filed@y.com", folder="Monday data")

    body = client.get("/api/v1/leads/folders").json()
    assert body["unfiled"] == 1
    assert body["total"] == 2
    assert {f["name"]: f["count"] for f in body["folders"]} == {"Monday data": 1}


def test_folder_catalog_counts_match_list_not_junk(tmp_path, monkeypatch):
    """Catalog counts = THE LIST, not the store: a hidden skip/junk dossier must
    never inflate the chips ("chip says 313 but the view shows 38").

    The leads list hides ``skip`` dossiers (dead domain / non-construction /
    sub-threshold) by default, so ``unfiled``/``total``/per-folder counts here
    must re-gate the same way and agree with what the list actually returns.
    """
    client = _setup(tmp_path, monkeypatch)
    store = leads_module._store
    store.save(_dossier("good@x.com", "x.com"))                  # actionable
    store.save(_dossier("junk@y.com", "y.com", score=1.0))       # skipped (low score)
    store.save(_dossier("filed@z.com", "z.com"))
    store.set_meta("filed@z.com", folder="Monday data")

    body = client.get("/api/v1/leads/folders").json()
    assert body["unfiled"] == 1
    assert body["total"] == 2
    assert {f["name"]: f["count"] for f in body["folders"]} == {"Monday data": 1}

    # Cross-check: chip counts == actual rows the list shows.
    all_view = client.get("/api/v1/leads", params={"folder": "*"}).json()
    assert {l["email"] for l in all_view} == {"good@x.com", "filed@z.com"}
    assert len(all_view) == body["total"]
    inbox_view = client.get("/api/v1/leads").json()
    assert {l["email"] for l in inbox_view} == {"good@x.com"}
    assert len(inbox_view) == body["unfiled"]


# ---------------------------------------------------------------------------
# Multi-user isolation (2026-09-12) — the catalog is per-account, like data.
# Root cause fixed: folders keyed on name ALONE, so every account saw every
# folder. Now keyed on (user_id, name); each account owns its groups.
# ---------------------------------------------------------------------------

def test_migration_old_singleuser_folders_table_rebuilds(tmp_path):
    """A pre-multiuser DB (folders keyed on name alone) is rebuilt in place:
    rows survive as legacy ('' owner) rows — nothing lost, nothing shared."""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE folders (
            name TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT INTO folders(name) VALUES ('Monday data')")
    conn.execute("INSERT INTO folders(name) VALUES ('Hot')")
    conn.commit()
    conn.close()

    store = LeadResearchStore(db_path=db)
    names = {f["name"] for f in store.list_folders()}  # legacy scope
    assert names == {"Monday data", "Hot"}
    # The table now carries the per-owner key.
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(folders)")}
    assert "user_id" in cols
    pk = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(folders)") if r[5]]
    assert pk == ["user_id", "name"]


def test_two_users_same_folder_name_each_sees_own(tmp_path):
    """Same-named folders are ALLOWED per account and never cross-visible."""
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    assert store.create_folder("Hot", user_id="u1") is True
    assert store.create_folder("Hot", user_id="u2") is True  # same name, other account

    u1 = {f["name"] for f in store.list_folders(user_id="u1")}
    u2 = {f["name"] for f in store.list_folders(user_id="u2")}
    assert u1 == {"Hot"} and u2 == {"Hot"}  # each sees their OWN row only

    u1_catalog = store.folder_catalog(user_id="u1")
    u2_catalog = store.folder_catalog(user_id="u2")
    assert [f["name"] for f in u1_catalog["folders"]] == ["Hot"]
    assert [f["name"] for f in u2_catalog["folders"]] == ["Hot"]
    assert u1_catalog["total"] == 0 and u2_catalog["total"] == 0


def test_created_folder_not_visible_to_other_accounts(tmp_path):
    """The bug being fixed: u1's folder used to show on EVERY account."""
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.create_folder("Monday data", user_id="u1")

    # u2's catalog is EMPTY — u1's group is invisible to them.
    assert store.list_folders(user_id="u2") == []
    assert store.folder_catalog(user_id="u2")["folders"] == []
    # u1 still sees their own.
    assert [f["name"] for f in store.list_folders(user_id="u1")] == ["Monday data"]


def test_backfill_attributes_folder_to_dossier_owner_only(tmp_path):
    """A folder name riding on leads self-heals into the OWNER's catalog —
    never into every account's."""
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.save(_dossier("a@x.com", "x.com"), user_id="u1")
    store.set_meta("a@x.com", folder="Hot")

    assert [f["name"] for f in store.list_folders(user_id="u1")] == ["Hot"]
    assert store.list_folders(user_id="u2") == []  # u2 never filed anything


def test_shared_lead_folder_shows_with_honest_count(tmp_path):
    """A shared lead (Phase 2: user's own search surfaced it, another user owns
    the dossier row) filed in the owner's folder still needs its place in the
    SHARING user's view — the catalog lists the name with this user's visible
    count so the chips always sum to ``total``."""
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    # u1 owns the dossier row; u2's search surfaced it (owner row).
    store.save(_dossier("shared@x.com", "x.com"), user_id="u1")
    store.set_meta("shared@x.com", folder="Dallas")
    store.add_owner("shared@x.com", "u2")

    catalog = store.folder_catalog(user_id="u2")
    assert {f["name"]: f["count"] for f in catalog["folders"]} == {"Dallas": 1}
    assert catalog["total"] == 1  # chips sum honestly to total


def test_admin_own_dashboard_sees_own_and_legacy_folders(tmp_path):
    """The admin's own dashboard lists own + legacy rows — but never another
    account's groups."""
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.create_folder("Legacy group")                     # '' owner
    store.create_folder("Admin group", user_id="admin1")
    store.create_folder("User group", user_id="u1")

    names = {f["name"] for f in store.list_folders(
        user_id="admin1", include_legacy=True)}
    assert names == {"Legacy group", "Admin group"}
    assert "User group" not in names


def test_rename_and_clear_never_touch_other_accounts_rows(tmp_path):
    """u1 renaming/clearing their folder leaves u2's same-named folder intact."""
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.create_folder("Hot", user_id="u1")
    store.create_folder("Hot", user_id="u2")
    store.save(_dossier("a@x.com", "x.com"), user_id="u1")
    store.set_meta("a@x.com", folder="Hot")
    store.save(_dossier("b@y.com", "y.com"), user_id="u2")
    store.set_meta("b@y.com", folder="Hot")

    # u1 renames: only u1's leads move, only u1's row swaps.
    assert store.rename_folder("Hot", "Priority", user_id="u1") == 1
    assert {f["name"]: f["count"] for f in store.list_folders(user_id="u1")} == {"Priority": 1}
    assert {f["name"]: f["count"] for f in store.list_folders(user_id="u2")} == {"Hot": 1}
    assert store.get_meta("b@y.com").folder == "Hot"  # u2's lead untouched

    # u2 clears: their lead is released, their row goes; u1 keeps "Priority".
    assert store.clear_folder("Hot", user_id="u2") == 1
    assert store.list_folders(user_id="u2") == []
    assert [f["name"] for f in store.list_folders(user_id="u1")] == ["Priority"]


def test_admin_drilldown_lists_that_users_folders(tmp_path):
    """folder_catalog(user_id=X, is_admin=False) — the admin panel's per-user
    summary shows exactly that account's groups + counts."""
    store = LeadResearchStore(db_path=str(tmp_path / "db.sqlite"))
    store.create_folder("Field work", user_id="u1")
    store.save(_dossier("a@x.com", "x.com"), user_id="u1")
    store.set_meta("a@x.com", folder="Field work")
    store.create_folder("Other", user_id="u2")

    catalog = store.folder_catalog(user_id="u1", is_admin=False)
    assert {f["name"]: f["count"] for f in catalog["folders"]} == {"Field work": 1}
    assert catalog["total"] == 1


# ---------------------------------------------------------------------------
# HTTP: per-account folder isolation
# ---------------------------------------------------------------------------

def _second_user_client(tmp_path, monkeypatch, username="otherguy"):
    """A second authed client sharing the same store/users DB as _setup's."""
    from app.auth.jwt import create_access_token
    import app.auth.dependencies as deps
    # Reuse the SAME users.db _authed_client created (deps._user_store is
    # already monkeypatched by _setup's _authed_client call).
    user_store = deps._user_store()
    user = user_store.create(username, f"{username}@example.com", "password")
    token = create_access_token(user.id, user.is_admin, username=user.username)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"}), user.id


def test_folders_http_per_account_isolation(tmp_path, monkeypatch):
    """Over HTTP: user A's created folder never appears on user B's rail,
    and B creating the same name gets their OWN empty group."""
    client_a = _setup(tmp_path, monkeypatch)
    client_b, _ = _second_user_client(tmp_path, monkeypatch)

    r = client_a.post("/api/v1/leads/folders", json={"name": "Monday data"})
    assert r.status_code == 201 and r.json()["created"] is True

    # B's rail: empty — A's folder is invisible to them.
    assert client_b.get("/api/v1/leads/folders").json()["folders"] == []

    # B creates the SAME name — their own group, not a duplicate error.
    r = client_b.post("/api/v1/leads/folders", json={"name": "Monday data"})
    assert r.status_code == 201 and r.json()["created"] is True

    rails = {
        who: {f["name"] for f in c.get("/api/v1/leads/folders").json()["folders"]}
        for who, c in (("a", client_a), ("b", client_b))
    }
    assert rails == {"a": {"Monday data"}, "b": {"Monday data"}}

    # A deleting their folder never touches B's.
    r = client_a.post("/api/v1/leads/organize/clear",
                      json={"kind": "folder", "value": "Monday data"})
    assert r.status_code == 200
    assert [f["name"] for f in client_b.get("/api/v1/leads/folders").json()["folders"]] == ["Monday data"]
    assert client_a.get("/api/v1/leads/folders").json()["folders"] == []