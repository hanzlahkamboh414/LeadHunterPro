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
            dead INTEGER NOT NULL DEFAULT 0
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
        "INSERT INTO pending_leads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("active", "active@x.test", "x.test", "X", "", "", "", "2026-09-07", 0),
            ("dead", "dead@x.test", "x.test", "X", "", "", "", "2026-09-07", 1),
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

    client = TestClient(app)
    response = client.get("/api/v1/admin/dashboard")

    assert response.status_code == 200
    assert response.json()["dossiers_total"] == 2


def test_admin_endpoint_rejects_missing_configured_api_key(tmp_path, monkeypatch):
    db_path = str(tmp_path / "lead_research.db")
    _make_db(db_path)
    monkeypatch.setattr(admin_module, "_reader", AdminReadRepository(db_path))
    monkeypatch.setattr(leads_module, "_manager", JobManager(db_path=str(tmp_path / "jobs.db")))
    monkeypatch.setattr(settings, "LEADS_API_KEY", "admin-secret")

    response = TestClient(app).get("/api/v1/admin/dashboard")

    assert response.status_code == 401
