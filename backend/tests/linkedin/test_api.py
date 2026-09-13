"""P4 — LinkedIn API contracts.

Any authenticated account reaches the vertical (it is an email-research
byproduct with no quota); serve is pool-only with an honest shortfall
reason; exclusivity applies at serve.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.linkedin import router as linkedin_router
from app.auth.models import UserStore
from app.linkedin.store import LinkedInLeadsStore


@pytest.fixture()
def tmp_stores(tmp_path):
    return (
        UserStore(db_path=str(tmp_path / "users.db")),
        LinkedInLeadsStore(db_path=str(tmp_path / "linkedin.db")),
    )


@pytest.fixture()
def client(tmp_stores, tmp_path, monkeypatch):
    user_store, li_store = tmp_stores
    app = FastAPI()
    app.include_router(linkedin_router, prefix="/api/v1")

    import app.api.v1.linkedin as linkedin_mod
    monkeypatch.setattr(linkedin_mod, "_store", li_store)

    import app.auth.dependencies as deps
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)

    # Hermetic auth toggle + activity store (same wiring as the phones API
    # tests — auth ON regardless of the real output/users.db).
    from app.auth import settings as auth_settings_module
    monkeypatch.setattr(
        auth_settings_module, "get_settings",
        lambda: auth_settings_module.AuthSettings(
            db_path=str(tmp_path / "users.db")),
    )
    from app.auth.activity import ActivityStore
    import app.auth.activity as activity_module
    monkeypatch.setattr(
        activity_module, "_activity_store",
        ActivityStore(db_path=str(tmp_path / "users.db")),
    )
    return TestClient(app)


def _token(user) -> str:
    from app.auth.jwt import create_access_token

    return create_access_token(
        user.id, user.is_admin, username=user.username, name=user.name,
        category=user.category,
    )


@pytest.fixture()
def make_user(tmp_stores):
    user_store, _ = tmp_stores

    def _make(username: str, category: str = "emails"):
        return user_store.create(
            username=username, email=f"{username}@x.com",
            password="secret123", category=category,
        )

    return _make


def _stock(li_store):
    li_store.add([{
        "person_name": "Jane Smith", "role": "Owner",
        "linkedin_url": "https://www.linkedin.com/in/jane-smith",
        "company_name": "Acme GC", "domain": "acmegc.com",
        "trade": "General Contractor", "location": "Vancouver, WA",
        "source": "email_research", "source_email": "owner@acmegc.com",
    }])


def test_every_category_reaches_the_vertical(client, make_user):
    """emails / phones / both — all authenticated accounts get in (a
    byproduct lane, not a signup-category product)."""
    for username in ("emailer", "phoner", "bothr"):
        user = make_user(username)
        resp = client.post(
            "/api/v1/linkedin/search",
            headers={"Authorization": f"Bearer {_token(user)}"},
            json={"trade": "GC", "target": 5},
        )
        assert resp.status_code == 200, (username, resp.text)


def test_search_serves_exclusively_and_reports_honestly(client, make_user,
                                                        tmp_stores):
    _, li_store = tmp_stores
    _stock(li_store)

    alice = make_user("alice")
    headers = {"Authorization": f"Bearer {_token(alice)}"}
    resp = client.post("/api/v1/linkedin/search", headers=headers,
                       json={"trade": "GC", "state": "WA", "target": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["served_from_pool"] == 1
    lead = body["leads"][0]
    assert lead["person_name"] == "Jane Smith"
    assert lead["linkedin_url"] == "https://www.linkedin.com/in/jane-smith"
    assert lead["trade"] == "gc"
    # The pool only held 1 of 5 — the shortfall is honest, never padded.
    assert "no live LinkedIn fetch" in body["reason"]

    # The lead is alice's now; bob's search gets nothing.
    mine = client.get("/api/v1/linkedin/leads", headers=headers).json()
    assert [l["person_name"] for l in mine] == ["Jane Smith"]
    bob = make_user("bob")
    bob_resp = client.post(
        "/api/v1/linkedin/search",
        headers={"Authorization": f"Bearer {_token(bob)}"},
        json={"trade": "GC", "target": 5},
    )
    assert bob_resp.json()["leads"] == []

    stats = client.get("/api/v1/linkedin/stats", headers=headers).json()
    assert stats["total"] == 1 and stats["mine"] == 1


def test_empty_pool_is_honest(client, make_user):
    user = make_user("solo")
    resp = client.post(
        "/api/v1/linkedin/search",
        headers={"Authorization": f"Bearer {_token(user)}"},
        json={"trade": "GC", "target": 5},
    )
    assert resp.status_code == 200
    assert resp.json()["leads"] == []
    assert "no live LinkedIn fetch" in resp.json()["reason"]


def test_requires_auth(client):
    resp = client.post("/api/v1/linkedin/search",
                       json={"trade": "GC", "target": 5})
    assert resp.status_code == 401
