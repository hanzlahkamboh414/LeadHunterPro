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
    return TestClient(app)


def _stock(store: PhoneLeadsStore, n: int = 1) -> None:
    """P7: the search is pure SQL — tests pre-stock the shared pool the way
    the harvester does, instead of faking an in-request SODA fetch."""
    store.add([{
        "phone": f"503111000{i}", "person_name": "SMITH, JANE",
        "business_name": "Acme GC", "trade_category": "GENERAL",
        "city": "SEATTLE", "state": "WA", "source": "wa_license",
        "license_status": "ACTIVE", "source_url": "https://data.wa.gov/x",
    } for i in range(1, n + 1)])


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


def test_phones_and_both_and_admin_pass_gate(client, make_user, tmp_stores):
    """The gate lets phones/both/admin THROUGH (200). The lead itself is
    EXCLUSIVE — the first claimant owns it, later searches in this test
    honestly get 0 (the pool only had one row)."""
    _, phone_store = tmp_stores
    _stock(phone_store)
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

def test_search_serves_and_records_activity(client, make_user, tmp_stores):
    _, phone_store = tmp_stores
    _stock(phone_store)
    user = make_user("alice", category="phones")
    headers = {"Authorization": f"Bearer {_token(user)}"}

    resp = client.post("/api/v1/phones/search", headers=headers,
                       json={"trade": "GC", "state": "WA", "city": "Seattle",
                             "target": 5})
    assert resp.status_code == 200
    body = resp.json()
    # P7: the serve is pure SQL — no in-request fetch, coverage still honest.
    assert body["served_from_pool"] == 1
    assert body["fetched_live"] == 0
    assert body["coverage"] == ["wa_license"]
    assert "harvester" in body["reason"]
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


def test_email_fields_flow_to_the_api(client, make_user, tmp_stores):
    """A freshly served lead is enrichment-pending; once the worker stamps a
    found email, both /phones/leads and a new search expose it honestly."""
    _, phone_store = tmp_stores
    _stock(phone_store)
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


# ---------------------------------------------------------------------------
# P7.5 — the calling workflow endpoints
# ---------------------------------------------------------------------------

def test_trade_less_search_is_the_default_flow(client, make_user, tmp_stores):
    """The P7.5 form: state + quantity only. No trade key at all — the pool
    serves mixed trades, coverage = anything stocking the state."""
    _, phone_store = tmp_stores
    phone_store.add([
        {"phone": "5031110001", "person_name": "SMITH, JANE",
         "business_name": "GC Co", "trade_category": "GENERAL",
         "city": "SEATTLE", "state": "WA", "source": "wa_license",
         "license_status": "ACTIVE", "source_url": "https://data.wa.gov/x"},
        {"phone": "5031110002", "person_name": "SMITH, JANE",
         "business_name": "Paint Co", "trade_category": "PAINTING/WALLCOVERING",
         "city": "SEATTLE", "state": "WA", "source": "wa_license",
         "license_status": "ACTIVE", "source_url": "https://data.wa.gov/x"},
    ])
    user = make_user("caller", category="phones")
    headers = {"Authorization": f"Bearer {_token(user)}"}
    resp = client.post("/api/v1/phones/search", headers=headers,
                       json={"state": "WA", "target": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["served_from_pool"] == 2
    assert body["coverage"] == ["wa_license"]
    trades = {l["trade"] for l in body["leads"]}
    assert trades == {"gc", "painting"}  # mixed sheet, each row labelled
    assert all("voicemail_count" in l for l in body["leads"])


def test_calling_workflow_actions_end_to_end(client, make_user, tmp_stores):
    """✓Lead / ☎Voicemail / 💾Store / 📝Note + My Leads/Contacts CRUD, over
    HTTP exactly as the screen drives them."""
    _, phone_store = tmp_stores
    _stock(phone_store, 2)
    user = make_user("caller", category="phones")
    bob = make_user("rival", category="phones")
    headers = {"Authorization": f"Bearer {_token(user)}"}
    bob_headers = {"Authorization": f"Bearer {_token(bob)}"}

    # 1) Trade-less search -> 2 numbers on the sheet.
    resp = client.post("/api/v1/phones/search", headers=headers,
                       json={"state": "WA", "target": 2}).json()
    leads = client.get("/api/v1/phones/leads", headers=headers).json()
    assert len(leads) == 2

    # 2) ☎Voicemail on the first: parked 14 days, released off the sheet.
    vm = client.post(f"/api/v1/phones/leads/{resp['leads'][0]['id']}/voicemail",
                     headers=headers)
    assert vm.status_code == 200
    assert vm.json() == {"retired": False, "voicemail_count": 1,
                         "cooldown_days": 14}
    assert len(client.get("/api/v1/phones/leads", headers=headers).json()) == 1

    # 3) 💾Store on the second: saved as a contact, stays claimed.
    st = client.post(f"/api/v1/phones/leads/{resp['leads'][1]['id']}/store",
                     headers=headers).json()
    assert st["retired"] is False
    assert len(client.get("/api/v1/phones/leads", headers=headers).json()) == 1

    # 4) 📝Note on the remaining sheet lead: auto-stored contact + note.
    sheet = client.get("/api/v1/phones/leads", headers=headers).json()
    nt = client.post(f"/api/v1/phones/leads/{sheet[0]['id']}/note",
                     headers=headers, json={"note": "call Tuesday"})
    assert nt.status_code == 200

    # 5) ✓Lead on it: snapshot saved, number retired for good.
    ld = client.post(f"/api/v1/phones/leads/{sheet[0]['id']}/lead",
                     headers=headers).json()
    assert ld["retired"] is True
    assert client.get("/api/v1/phones/leads", headers=headers).json() == []
    # The retired number is suppressed: even the harvester's add() refuses.
    counts = phone_store.add([{
        "phone": resp["leads"][1]["phone"], "person_name": "SMITH, JANE",
        "business_name": "Acme GC", "trade_category": "GENERAL",
        "city": "SEATTLE", "state": "WA", "source": "wa_license",
        "license_status": "ACTIVE", "source_url": "https://data.wa.gov/x",
    }])
    assert counts["suppressed"] == 1

    # 6) My Leads / My Contacts: the saved rows, notes editable, deletable.
    #    (The 📝Note upserted the SAME contact row the earlier 💾Store made —
    #    saved rows are keyed by (user, phone, kind), so one number is one
    #    contact per user, refreshed — never duplicated.)
    saved = client.get("/api/v1/phones/saved", headers=headers).json()
    kinds = sorted(s["kind"] for s in saved)
    assert kinds == ["contact", "lead"]
    contact = next(s for s in saved if s["kind"] == "contact")
    assert contact["note"] == "call Tuesday"
    saved_leads = client.get("/api/v1/phones/saved?kind=lead",
                             headers=headers).json()
    assert len(saved_leads) == 1
    target = next(s for s in saved if s["kind"] == "lead")
    ok = client.put(f"/api/v1/phones/saved/{target['id']}/note",
                    headers=headers, json={"note": "project in October"})
    assert ok.status_code == 200
    assert client.get("/api/v1/phones/saved?kind=lead",
                      headers=headers).json()[0]["note"] == "project in October"
    # Another user's saved rows are invisible — and undeletable.
    assert client.get("/api/v1/phones/saved", headers=bob_headers).json() == []
    assert client.delete(f"/api/v1/phones/saved/{target['id']}",
                         headers=bob_headers).status_code == 404
    assert client.delete(f"/api/v1/phones/saved/{target['id']}",
                         headers=headers).status_code == 200
    assert len(client.get("/api/v1/phones/saved?kind=lead",
                          headers=headers).json()) == 0


def test_actions_require_ownership_and_category(client, make_user, tmp_stores):
    """The action endpoints are owner-only (404 for anyone else's lead) and
    the category gate covers the whole calling workflow, not just search."""
    _, phone_store = tmp_stores
    _stock(phone_store)
    alice = make_user("alice", category="phones")
    bob = make_user("bob", category="phones")

    a_headers = {"Authorization": f"Bearer {_token(alice)}"}
    b_headers = {"Authorization": f"Bearer {_token(bob)}"}
    lead = client.post("/api/v1/phones/search", headers=a_headers,
                       json={"state": "WA", "target": 1}).json()["leads"][0]

    # Bob (a phones user, but NOT the owner) cannot act on alice's lead.
    for action in ("lead", "voicemail", "store"):
        resp = client.post(f"/api/v1/phones/leads/{lead['id']}/{action}",
                           headers=b_headers)
        assert resp.status_code == 404
    # ...and the row is untouched — still alice's, still on her sheet.
    assert len(client.get("/api/v1/phones/leads", headers=a_headers).json()) == 1

    # An emails-only account is gated out of the calling workflow too.
    emailer = make_user("emailer", category="emails")
    e_headers = {"Authorization": f"Bearer {_token(emailer)}"}
    assert client.post(f"/api/v1/phones/leads/{lead['id']}/voicemail",
                       headers=e_headers).status_code == 403
    assert client.get("/api/v1/phones/saved", headers=e_headers).status_code == 403
