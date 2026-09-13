"""P3 — the signup category question (auth domain).

* ``UserStore.create(category=...)`` persists the vertical answer; anything
  that is not emails/phones/both falls back to both (a bad client payload
  can never lock an account out of everything).
* Existing DBs gain the column via the guarded additive ALTER with default
  ``both`` — nobody loses access they already had.
* The signup endpoint accepts + returns the category; login and /me expose
  it; the JWT carries it as a claim the frontend decodes.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.auth import router as auth_router
from app.auth.jwt import decode_access_token
from app.auth.models import UserStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")

    import app.api.v1.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_store", user_store)

    import app.auth.dependencies as deps
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)

    from app.auth.activity import ActivityStore
    import app.auth.activity as activity_module
    monkeypatch.setattr(
        activity_module, "_activity_store",
        ActivityStore(db_path=str(tmp_path / "users.db")),
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def test_create_persists_category(tmp_path):
    store = UserStore(db_path=str(tmp_path / "u.db"))
    assert store.create("a", "a@x.com", "pw1234", category="phones").category \
        == "phones"
    assert store.get_by_username("a").category == "phones"


def test_create_defaults_and_falls_back_to_both(tmp_path):
    store = UserStore(db_path=str(tmp_path / "u.db"))
    assert store.create("d", "d@x.com", "pw1234").category == "both"
    assert store.create("bad", "bad@x.com", "pw1234", category="gold").category \
        == "both"


def test_existing_db_migrates_to_both(tmp_path):
    """A pre-P3 users.db (no category column) migrates additively — every
    existing account keeps full access (both)."""
    import sqlite3

    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE, "
        "email TEXT UNIQUE, password_hash TEXT, is_admin INTEGER, "
        "created_at TEXT, name TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO users VALUES ('legacy1', 'old', 'old@x.com', 'h', 0, "
        "'2026-01-01', 'Old')"
    )
    conn.commit()
    conn.close()

    store = UserStore(db_path=db)
    assert store.get_by_username("old").category == "both"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_signup_accepts_and_returns_category(client):
    res = client.post("/api/v1/auth/signup", json={
        "username": "catphone", "email": "cp@x.com", "password": "pw1234",
        "name": "Cat Phone", "category": "phones",
    })
    assert res.status_code == 201
    body = res.json()
    assert body["category"] == "phones"
    # The token carries the claim (the frontend decodes it client-side).
    assert decode_access_token(body["token"])["category"] == "phones"


def test_signup_defaults_to_both(client):
    res = client.post("/api/v1/auth/signup", json={
        "username": "catboth", "email": "cb@x.com", "password": "pw1234",
    })
    assert res.json()["category"] == "both"


def test_login_and_me_expose_category(client):
    client.post("/api/v1/auth/signup", json={
        "username": "catmail", "email": "cm@x.com", "password": "pw1234",
        "category": "emails",
    })
    login = client.post("/api/v1/auth/login", json={
        "username": "catmail", "password": "pw1234",
    })
    assert login.json()["category"] == "emails"
    me = client.get("/api/v1/auth/me", headers={
        "Authorization": f"Bearer {login.json()['token']}",
    })
    assert me.json()["category"] == "emails"
