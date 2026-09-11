"""Auth system tests — signup, login, JWT, password reset, admin guard."""

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token, decode_access_token
from app.auth.models import UserStore
from app.api.v1.auth import router as auth_router


@pytest.fixture()
def tmp_user_store(tmp_path):
    """A fresh UserStore using a temp DB for each test."""
    return UserStore(db_path=str(tmp_path / "users.db"))


@pytest.fixture()
def client(tmp_user_store, tmp_path, monkeypatch):
    """FastAPI test client wired to a temp UserStore."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")

    # Patch the module-level _store singleton (auth endpoints)
    import app.api.v1.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_store", tmp_user_store)

    # Patch the dependencies lazy singleton (get_current_user) to the SAME
    # temp store, so a token minted by signup resolves in /me.
    import app.auth.dependencies as deps
    monkeypatch.setattr(deps, "_user_store", lambda: tmp_user_store)

    # login/signup/logout record activity via the get_activity() singleton —
    # point it at the SAME temp db, or every test run pollutes the real one.
    from app.auth.activity import ActivityStore
    import app.auth.activity as activity_module
    monkeypatch.setattr(
        activity_module, "_activity_store",
        ActivityStore(db_path=str(tmp_path / "users.db")),
    )

    return TestClient(app)


class TestJWT:
    def test_create_and_decode(self):
        token = create_access_token("user123", is_admin=False)
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["user_id"] == "user123"
        assert payload["is_admin"] is False

    def test_admin_token(self):
        token = create_access_token("admin1", is_admin=True)
        payload = decode_access_token(token)
        assert payload["is_admin"] is True

    def test_invalid_token(self):
        assert decode_access_token("garbage.token.here") is None

    def test_empty_token(self):
        assert decode_access_token("") is None


class TestUserStore:
    def test_create_and_get(self, tmp_user_store):
        user = tmp_user_store.create("alice", "alice@example.com", "pass123")
        assert user.username == "alice"
        assert user.email == "alice@example.com"
        assert user.is_admin is False
        fetched = tmp_user_store.get_by_username("alice")
        assert fetched is not None
        assert fetched.id == user.id

    def test_duplicate_username(self, tmp_user_store):
        tmp_user_store.create("bob", "bob@example.com", "pass123")
        with pytest.raises(ValueError, match="Username already taken"):
            tmp_user_store.create("bob", "bob2@example.com", "pass456")

    def test_verify_password_correct(self, tmp_user_store):
        tmp_user_store.create("carol", "carol@example.com", "secret")
        user = tmp_user_store.verify_password("carol", "secret")
        assert user is not None
        assert user.username == "carol"

    def test_verify_password_wrong(self, tmp_user_store):
        tmp_user_store.create("dave", "dave@example.com", "secret")
        assert tmp_user_store.verify_password("dave", "wrong") is None

    def test_verify_nonexistent(self, tmp_user_store):
        assert tmp_user_store.verify_password("nobody", "pass") is None

    def test_reset_password(self, tmp_user_store):
        tmp_user_store.create("eve", "eve@example.com", "old_pass")
        assert tmp_user_store.reset_password("eve", "new_pass") is True
        assert tmp_user_store.verify_password("eve", "new_pass") is not None
        assert tmp_user_store.verify_password("eve", "old_pass") is None

    def test_reset_nonexistent(self, tmp_user_store):
        assert tmp_user_store.reset_password("nobody", "pass") is False

    def test_ensure_admin_creates(self, tmp_user_store):
        admin = tmp_user_store.ensure_admin()
        assert admin.username == "admin4269"
        assert admin.is_admin is True

    def test_ensure_admin_idempotent(self, tmp_user_store):
        a1 = tmp_user_store.ensure_admin()
        a2 = tmp_user_store.ensure_admin()
        assert a1.id == a2.id


class TestSignupEndpoint:
    def test_signup_success(self, client):
        res = client.post("/api/v1/auth/signup", json={
            "username": "newuser",
            "email": "new@example.com",
            "password": "pass123",
        })
        assert res.status_code == 201
        data = res.json()
        assert data["username"] == "newuser"
        assert "token" in data

    def test_signup_duplicate(self, client):
        client.post("/api/v1/auth/signup", json={
            "username": "dupe",
            "email": "a@example.com",
            "password": "pass",
        })
        res = client.post("/api/v1/auth/signup", json={
            "username": "dupe",
            "email": "b@example.com",
            "password": "pass",
        })
        assert res.status_code == 409


class TestLoginEndpoint:
    def test_login_success(self, client):
        client.post("/api/v1/auth/signup", json={
            "username": "logintest",
            "email": "login@example.com",
            "password": "mypassword",
        })
        res = client.post("/api/v1/auth/login", json={
            "username": "logintest",
            "password": "mypassword",
        })
        assert res.status_code == 200
        data = res.json()
        assert "token" in data
        assert data["username"] == "logintest"

    def test_login_wrong_password(self, client):
        client.post("/api/v1/auth/signup", json={
            "username": "wrongpw",
            "email": "wp@example.com",
            "password": "correct",
        })
        res = client.post("/api/v1/auth/login", json={
            "username": "wrongpw",
            "password": "incorrect",
        })
        assert res.status_code == 401

    def test_login_nonexistent(self, client):
        res = client.post("/api/v1/auth/login", json={
            "username": "ghost",
            "password": "pass",
        })
        assert res.status_code == 401


class TestResetPasswordEndpoint:
    def test_reset_success(self, client):
        client.post("/api/v1/auth/signup", json={
            "username": "resetme",
            "email": "reset@example.com",
            "password": "oldpw",
        })
        res = client.post("/api/v1/auth/reset-password", json={
            "username": "resetme",
            "new_password": "newpw",
        })
        assert res.status_code == 200
        assert res.json()["success"] is True

    def test_reset_nonexistent(self, client):
        res = client.post("/api/v1/auth/reset-password", json={
            "username": "nobody",
            "new_password": "password",
        })
        assert res.status_code == 404


class TestMeEndpoint:
    def test_me_authenticated(self, client):
        signup = client.post("/api/v1/auth/signup", json={
            "username": "metest",
            "email": "me@example.com",
            "password": "pass",
        })
        token = signup.json()["token"]
        res = client.get("/api/v1/auth/me", headers={
            "Authorization": f"Bearer {token}",
        })
        assert res.status_code == 200
        assert res.json()["username"] == "metest"

    def test_me_no_token(self, client):
        res = client.get("/api/v1/auth/me")
        assert res.status_code == 401
