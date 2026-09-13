"""P3 — Phones API + the signup category gate.

The category question at signup ("emails" | "phones" | "both") gates the
phones vertical: an emails-only account gets 403, phones/both/admin get in.
Existing accounts default to "both" (the additive ALTER) so nobody loses
access they already had.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.phones import router as phones_router
from app.auth.models import UserStore
from app.phones.store import PhoneLeadsStore


@pytest.fixture()
def tmp_stores(tmp_path):
    return (
        UserStore(db_path=str(tmp_path / "users.db")),
        PhoneLeadsStore(db_path=str(tmp_path / "phones.db")),
    )


@pytest.fixture()
def client(tmp_stores, tmp_path, monkeypatch):
    """API client wired to temp stores + a fake SODA fetcher (hermetic)."""
    user_store, phone_store = tmp_stores
    app = FastAPI()
    app.include_router(phones_router, prefix="/api/v1")

    import app.api.v1.phones as phones_mod
    monkeypatch.setattr(phones_mod, "_store", phone_store)

    import app.auth.dependencies as deps
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)

    # Hermetic auth toggle: auth ON regardless of the real output/users.db.
    from app.auth import settings as auth_settings_module
    monkeypatch.setattr(
        auth_settings_module, "get_settings",
        lambda: auth_settings_module.AuthSettings(
            db_path=str(tmp_path / "users.db")),
    )

    # login/signup record activity via this singleton — point it at the
    # temp db so the run never pollutes the real one.
    from app.auth.activity import ActivityStore
    import app.auth.activity as activity_module
    monkeypatch.setattr(
        activity_module, "_activity_store",
        ActivityStore(db_path=str(tmp_path / "users.db")),
    )

    # Hermetic: the SODA fetcher returns one WA gc row.
    from app.discovery.sources.status import SourceStatus
    from app.phones import service as phones_service

    def _fake_fetch(source_id, slug, city="", limit=200):
        records = [{
            "phone": "5031110001", "person_name": "SMITH, JANE",
            "business_name": "Acme GC", "trade_category": "GENERAL",
            "city": "SEATTLE", "state": "WA", "source": "wa_license",
            "license_status": "ACTIVE", "source_url": "https://data.wa.gov/x",
        }]
        return SourceStatus.SUCCESS, records, {"source": source_id}

    monkeypatch.setattr(phones_service, "fetch_license_records", _fake_fetch)
    return TestClient(app)


def _token(user) -> str:
    """Mint a JWT for a real user row (get_current_user resolves via store)."""
    from app.auth.jwt import create_access_token

    return create_access_token(
        user.id, user.is_admin, username=user.username, name=user.name,
        category=user.category,
    )


@pytest.fixture()
def make_user(tmp_stores):
    """Create a real user row in the temp store so get_current_user resolves."""
    user_store, _ = tmp_stores

    def _make(username: str, category: str = "both", is_admin: bool = False):
        if is_admin:
            return user_store.ensure_admin(username="root", password="x")
        return user_store.create(
            username=username, email=f"{username}@x.com",
            password="secret123", category=category,
        )

    return _make


# ---------------------------------------------------------------------------
# the category gate
# ---------------------------------------------------------------------------

def test_emails_only_account_gets_403(client, make_user):
    user = make_user("emailer", category="emails")
    resp = client.post("/api/v1/phones/search",
                       headers={"Authorization": f"Bearer {_token(user)}"},
                       json={"trade": "GC", "state": "WA", "target": 5})
    assert resp.status_code == 403
    assert "Emails vertical only" in resp.json()["detail"]


def test_phones_and_both_and_admin_pass_gate(client, make_user):
    """The gate lets phones/both/admin THROUGH (200). The lead itself is
    EXCLUSIVE — the first claimant owns it, later searches in this test
    honestly get 0 (the pool only had one row)."""
    served = []
    for username, category, admin in [
        ("phoner", "phones", False), ("bothr", "both", False), ("root", "", True),
    ]:
        user = make_user(username, category=category, is_admin=admin)
        resp = client.post("/api/v1/phones/search",
                           headers={"Authorization": f"Bearer {_token(user)}"},
                           json={"trade": "GC", "state": "WA", "target": 5})
        assert resp.status_code == 200, (username, resp.text)
        served.append(len(resp.json()["leads"]))
    assert served == [1, 0, 0]  # first user claimed the only row


# ---------------------------------------------------------------------------
# search + leads + stats endpoints
# ---------------------------------------------------------------------------

def test_search_serves_and_records_activity(client, make_user):
    user = make_user("alice", category="phones")
    headers = {"Authorization": f"Bearer {_token(user)}"}

    resp = client.post("/api/v1/phones/search", headers=headers,
                       json={"trade": "GC", "state": "WA", "city": "Seattle",
                             "target": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["fetched_live"] == 1
    assert body["coverage"] == ["wa_license"]
    lead = body["leads"][0]
    assert lead["phone"] == "+15031110001"
    assert lead["trade"] == "gc"
    assert lead["person_name"] == "Jane Smith"

    # The lead is now alice's, visible on GET /phones/leads...
    leads = client.get("/api/v1/phones/leads", headers=headers).json()
    assert [l["phone"] for l in leads] == ["+15031110001"]
    # ...and the stats endpoint reports the honest pool inventory.
    stats = client.get("/api/v1/phones/stats", headers=headers).json()
    assert stats["total"] == 1 and stats["mine"] == 1


def test_search_target_cap_for_users(client, make_user):
    user = make_user("capped", category="phones")
    resp = client.post("/api/v1/phones/search",
                       headers={"Authorization": f"Bearer {_token(user)}"},
                       json={"trade": "GC", "target": 5000})
    assert resp.status_code == 422
    assert "1000" in resp.json()["detail"]


def test_requires_auth(client):
    resp = client.post("/api/v1/phones/search",
                       json={"trade": "GC", "target": 5})
    assert resp.status_code == 401


def test_email_fields_flow_to_the_api(client, make_user):
    """A freshly served lead is enrichment-pending; once the worker stamps a
    found email, both /phones/leads and a new search expose it honestly."""
    user = make_user("caller", category="phones")
    headers = {"Authorization": f"Bearer {_token(user)}"}

    resp = client.post("/api/v1/phones/search", headers=headers,
                       json={"trade": "GC", "state": "WA", "target": 5})
    lead = resp.json()["leads"][0]
    assert lead["email"] == ""
    assert lead["email_status"] == "pending"

    # Simulate the background enricher's write on the shared store.
    import app.api.v1.phones as phones_mod
    phones_mod._store.set_enrichment(
        lead["id"], email="info@acme.com",
        email_source="website", website="https://acme.com",
    )

    leads = client.get("/api/v1/phones/leads", headers=headers).json()
    assert leads[0]["email"] == "info@acme.com"
    assert leads[0]["email_source"] == "website"
    assert leads[0]["website"] == "https://acme.com"
    assert leads[0]["email_status"] == "found"
