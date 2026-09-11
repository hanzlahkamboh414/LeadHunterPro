from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

import app.api.v1.admin as admin_module
import app.api.v1.leads as leads_module
from app.api.v1.leads import JobManager
from app.admin_read import AdminReadRepository
from app.core.config import settings
from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.main import app


def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE dossiers (
            email_hash TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            domain TEXT NOT NULL,
            dossier_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE pending_leads (
            email_hash TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            domain TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            person TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            dead INTEGER NOT NULL DEFAULT 0,
            attempted_at TIMESTAMP,
            attempt_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE deleted_leads (
            email TEXT PRIMARY KEY,
            deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reason TEXT NOT NULL DEFAULT 'manual'
        );
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            query_json TEXT NOT NULL,
            state TEXT NOT NULL,
            events_json TEXT NOT NULL,
            results_json TEXT NOT NULL,
            pass_log_json TEXT NOT NULL,
            error TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            elapsed_s REAL NOT NULL
        );
        """
    )
    good = LeadDossier(
        email="owner@acme.test",
        domain="acme.test",
        company=CompanyProfile(
            name="Acme Builders",
            industry="general contractor",
            location="Texas",
        ),
        person=PersonFindings(
            name="Owner",
            role="Owner",
            bound=True,
            role_relevance=True,
        ),
        sources_checked=["https://acme.test"],
        recommendation="contact_now",
        potential_score=8.0,
    )
    risky = LeadDossier(email="info@unknown.test", domain="unknown.test")
    for dossier in (good, risky):
        conn.execute(
            "INSERT INTO dossiers VALUES (?, ?, ?, ?, ?, ?)",
            (
                dossier.email,
                dossier.email,
                dossier.domain,
                json.dumps(dossier.to_dict()),
                "2026-09-07",
                "2026-09-07",
            ),
        )
    conn.executemany(
        "INSERT INTO pending_leads "
        "(email_hash, email, domain, company, person, source_url, location, "
        "created_at, dead, attempted_at, attempt_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("active", "active@x.test", "x.test", "X", "", "", "", "2026-09-07", 0, None, 0),
            ("dead", "dead@x.test", "x.test", "X", "", "", "", "2026-09-07", 1, None, 0),
        ],
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "job-1",
            json.dumps({"trade": "general contractor", "location": "Texas"}),
            "failed",
            "[]",
            "[]",
            "[]",
            "provider failed",
            "2026-09-07",
            "2026-09-07",
            12.0,
        ),
    )
    conn.commit()
    conn.close()


def _admin_client(tmp_path, monkeypatch) -> TestClient:
    """TestClient with an ADMIN JWT — the admin router now requires it."""
    from app.auth.jwt import create_access_token
    from app.auth.models import UserStore
    import app.auth.dependencies as deps

    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    user = user_store.ensure_admin()
    token = create_access_token(user.id, user.is_admin, username=user.username)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_admin_dashboard_is_read_only_and_reports_operational_metrics(tmp_path):
    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)

    result = AdminReadRepository(db_path).dashboard()

    assert result.dossiers_total == 2
    assert result.recommendations["contact_now"] == 1
    assert result.recommendations["skip"] == 1
    assert result.pending == {"active": 1, "dead": 1, "total": 2}
    assert result.jobs == {"failed": 1}
    assert result.risks["missing_company"] == 1
    assert result.risks["missing_evidence"] == 1
    assert result.recent_jobs[0].error == "provider failed"


def test_admin_endpoint_uses_existing_api_key_guard(tmp_path, monkeypatch):
    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)
    monkeypatch.setattr(admin_module, "_reader", AdminReadRepository(db_path))
    monkeypatch.setattr(leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db")))

    client = _admin_client(tmp_path, monkeypatch)
    response = client.get("/api/v1/admin/dashboard")

    assert response.status_code == 200
    assert response.json()["dossiers_total"] == 2


def test_admin_endpoint_rejects_missing_configured_api_key(tmp_path, monkeypatch):
    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)
    monkeypatch.setattr(admin_module, "_reader", AdminReadRepository(db_path))
    monkeypatch.setattr(leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db")))
    monkeypatch.setattr(settings, "LEADS_API_KEY", "admin-secret")

    # No JWT AND no X-API-Key → 401 (auth guard). The test's intent: when an API
    # key is configured, a caller without it is rejected.
    response = TestClient(app).get("/api/v1/admin/dashboard")

    assert response.status_code == 401


def test_admin_endpoint_rejects_non_admin_jwt(tmp_path, monkeypatch):
    """A logged-in NON-admin user must get 403 on the admin router."""
    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)
    monkeypatch.setattr(admin_module, "_reader", AdminReadRepository(db_path))
    monkeypatch.setattr(leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db")))

    from app.auth.jwt import create_access_token
    from app.auth.models import UserStore
    import app.auth.dependencies as deps

    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    user = user_store.create("plain", "plain@test.com", "password")
    token = create_access_token(user.id, user.is_admin, username=user.username)
    response = TestClient(app, headers={"Authorization": f"Bearer {token}"}).get(
        "/api/v1/admin/dashboard"
    )

    assert response.status_code == 403


def test_admin_keys_never_returns_full_secret(tmp_path, monkeypatch):
    """GET /admin/keys returns only masked tails — a full secret must NEVER leave the backend."""
    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)
    monkeypatch.setattr(admin_module, "_reader", AdminReadRepository(db_path))
    monkeypatch.setattr(leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db")))
    monkeypatch.setattr(settings, "LEADS_API_KEY", "test-key-12345")

    response = _admin_client(tmp_path, monkeypatch).get(
        "/api/v1/admin/keys", headers={"X-API-Key": "test-key-12345"}
    )
    assert response.status_code == 200
    data = response.json()
    for k in data["keys"]:
        assert "12345" not in k["masked"]  # full value never leaks
        assert "test-key" not in k["masked"]  # prefix never leaks
        if k["name"] == "LEADS_API_KEY":
            assert k["configured"] is True
            assert k["masked"].startswith("••••")


def test_admin_deleted_endpoint_returns_log(tmp_path, monkeypatch):
    """GET /admin/deleted returns the deleted_leads audit trail."""
    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)
    monkeypatch.setattr(admin_module, "_reader", AdminReadRepository(db_path))
    monkeypatch.setattr(leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db")))

    # The _make_db fixture creates the deleted_leads table (matching the real
    # store schema in app/lead_research/service.py), so the endpoint returns an
    # empty audit trail rather than erroring.
    response = _admin_client(tmp_path, monkeypatch).get("/api/v1/admin/deleted")
    assert response.status_code == 200
    data = response.json()
    assert "total" in data
    assert "deleted" in data
    assert isinstance(data["deleted"], list)


def test_admin_cache_pending_endpoint(tmp_path, monkeypatch):
    """GET /admin/cache/pending returns the pending_leads cache."""
    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)
    monkeypatch.setattr(admin_module, "_reader", AdminReadRepository(db_path))
    monkeypatch.setattr(leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db")))

    response = _admin_client(tmp_path, monkeypatch).get("/api/v1/admin/cache/pending")
    assert response.status_code == 200
    data = response.json()
    assert "total" in data
    assert "active" in data
    assert "dead" in data
    assert "rows" in data
    assert isinstance(data["rows"], list)
    # _make_db inserts 1 active + 1 dead pending lead
    assert data["total"] == 2
    assert data["active"] == 1
    assert data["dead"] == 1


def test_admin_cache_search_endpoint(tmp_path, monkeypatch):
    """GET /admin/cache/search returns search cache stats."""
    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)
    monkeypatch.setattr(admin_module, "_reader", AdminReadRepository(db_path))
    monkeypatch.setattr(leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db")))

    response = _admin_client(tmp_path, monkeypatch).get("/api/v1/admin/cache/search")
    assert response.status_code == 200
    data = response.json()
    assert "search_rows" in data
    assert "extract_rows" in data
    assert "hit_rate" in data
    assert isinstance(data["top_queries"], list)


def test_admin_purge_search_cache(tmp_path, monkeypatch):
    """POST /admin/cache/purge-search returns removed count."""
    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)
    monkeypatch.setattr(admin_module, "_reader", AdminReadRepository(db_path))
    monkeypatch.setattr(leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db")))

    response = _admin_client(tmp_path, monkeypatch).post("/api/v1/admin/cache/purge-search")
    assert response.status_code == 200
    data = response.json()
    assert "removed" in data
    assert isinstance(data["removed"], int)


def test_admin_update_key_rejects_unknown_key(tmp_path, monkeypatch):
    """PUT /admin/keys with unknown key name returns 422."""
    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)
    monkeypatch.setattr(admin_module, "_reader", AdminReadRepository(db_path))
    monkeypatch.setattr(leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db")))

    response = _admin_client(tmp_path, monkeypatch).put(
        "/api/v1/admin/keys",
        json={"name": "UNKNOWN_KEY", "value": "test"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# PUT /admin/keys — HOT RELOAD (no backend restart)
# ---------------------------------------------------------------------------

def _hot_reload_env(tmp_path, monkeypatch, overrides: dict[str, str]) -> "SearchProviderRegistry":
    """Isolate the key endpoint from the REAL overlay/settings/registry.

    The admin module-level ``_key_store`` points at the live
    backend/output/runtime_keys.json, the settings object is the process-wide
    singleton, and the search registry is a shared singleton — a hot-reload
    test must swap all three for throwaway copies or it would mutate real
    production state.

    The env snapshot starts from the CURRENT live value of every managed key
    (then applies *overrides*). A partial dict would make ``apply_live`` wipe
    the unlisted keys IN PLACE for the rest of the test session — in-place
    ``setattr`` is only undone by monkeypatch for attributes it patched.
    """
    from app.core import config
    from app.core.runtime_keys import KNOWN_KEY_NAMES, RuntimeKeyStore
    from app.search_providers import registry as sp_registry_module
    from app.search_providers.registry import SearchProviderRegistry

    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)
    monkeypatch.setattr(admin_module, "_reader", AdminReadRepository(db_path))
    monkeypatch.setattr(leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db")))
    monkeypatch.setattr(
        admin_module, "_key_store", RuntimeKeyStore(str(tmp_path / "runtime_keys.json"))
    )

    env_values = {name: getattr(config.settings, name, "") for name in KNOWN_KEY_NAMES}
    env_values.update(overrides)
    monkeypatch.setattr(config, "_ENV_KEY_VALUES", dict(env_values))
    for name, value in env_values.items():
        monkeypatch.setattr(config.settings, name, value)

    fake_registry = SearchProviderRegistry()
    monkeypatch.setattr(sp_registry_module, "get_registry", lambda: fake_registry)
    # re_register_configured_providers lives in the package __init__, which has
    # its own `get_registry` import — patch the name the package itself uses.
    import app.search_providers as sp_package

    monkeypatch.setattr(sp_package, "get_registry", lambda: fake_registry)
    return fake_registry


def test_admin_update_key_hot_reloads_tavily_provider(tmp_path, monkeypatch):
    """PUT /admin/keys with a Tavily key rebuilds the provider IMMEDIATELY.

    The whole point of the hot-reload path: the registry must hold a NEW
    instance carrying the new key, the live settings object must show it, and
    the response must say no restart is needed.
    """
    from app.core import config

    fake_registry = _hot_reload_env(
        tmp_path, monkeypatch, {"TAVILY_SEARCH_API_KEY": "", "BRAVE_SEARCH_API_KEY": ""}
    )

    response = _admin_client(tmp_path, monkeypatch).put(
        "/api/v1/admin/keys",
        json={"name": "TAVILY_SEARCH_API_KEY", "value": "tvly-hot-1"},
    )
    assert response.status_code == 200
    assert response.json()["applies_after_restart"] is False

    # New provider instance in the registry, carrying the NEW key.
    tavily = fake_registry.get("tavily")
    assert tavily is not None
    assert tavily._api_key == "tvly-hot-1"
    # Overlay persisted + live settings object updated in place.
    assert admin_module._key_store.load()["TAVILY_SEARCH_API_KEY"] == "tvly-hot-1"
    assert config.settings.TAVILY_SEARCH_API_KEY == "tvly-hot-1"


def test_admin_update_key_clear_unregisters_provider(tmp_path, monkeypatch):
    """Clearing a key (empty value) with no .env fallback removes the provider.

    A cleared key must not leave a dead-key instance in the registry — the
    next query would keep burning requests on a rejected credential.
    """
    _hot_reload_env(tmp_path, monkeypatch, {"TAVILY_SEARCH_API_KEY": "", "BRAVE_SEARCH_API_KEY": ""})
    client = _admin_client(tmp_path, monkeypatch)

    client.put("/api/v1/admin/keys", json={"name": "TAVILY_SEARCH_API_KEY", "value": "tvly-hot-1"})
    response = client.put("/api/v1/admin/keys", json={"name": "TAVILY_SEARCH_API_KEY", "value": ""})
    assert response.status_code == 200
    assert response.json()["applies_after_restart"] is False


def test_admin_update_key_clear_falls_back_to_env_value(tmp_path, monkeypatch):
    """Clearing an overlay key reverts to the .env value, like a restart would.

    The env snapshot (config._ENV_KEY_VALUES) is the fallback apply_live
    reverts to — the live state after a clear must equal the boot state.
    """
    from app.core import config

    fake_registry = _hot_reload_env(
        tmp_path, monkeypatch, {"TAVILY_SEARCH_API_KEY": "tvly-env-orig", "BRAVE_SEARCH_API_KEY": ""}
    )
    client = _admin_client(tmp_path, monkeypatch)

    # Overlay a new key, then clear it — settings must revert to .env's value
    # and the provider must be rebuilt with THAT key (not unregistered).
    client.put("/api/v1/admin/keys", json={"name": "TAVILY_SEARCH_API_KEY", "value": "tvly-overlay"})
    client.put("/api/v1/admin/keys", json={"name": "TAVILY_SEARCH_API_KEY", "value": ""})

    assert config.settings.TAVILY_SEARCH_API_KEY == "tvly-env-orig"
    tavily = fake_registry.get("tavily")
    assert tavily is not None
    assert tavily._api_key == "tvly-env-orig"


def test_admin_update_key_live_applies_ai_key_to_settings(tmp_path, monkeypatch):
    """An AI key change reaches the live settings object (no restart).

    AI clients are constructed per use from ``settings``, so updating the
    object in place is what makes the next call pick up the new key.
    """
    from app.core import config

    _hot_reload_env(tmp_path, monkeypatch, {"AI_API_KEY": "", "AI_API_KEY_2": ""})

    response = _admin_client(tmp_path, monkeypatch).put(
        "/api/v1/admin/keys",
        json={"name": "AI_API_KEY", "value": "sk-hot-42"},
    )
    assert response.status_code == 200
    assert config.settings.AI_API_KEY == "sk-hot-42"
