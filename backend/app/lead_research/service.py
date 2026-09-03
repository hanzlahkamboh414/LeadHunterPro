"""AI Lead Research — Service (store + agent + persist).

Ties the AILeadResearchAgent into a service that can:
- research(email, domain) → LeadDossier + persist to SQLite
- get(email) → stored LeadDossier or None
- list_leads() → all stored dossiers
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from app.lead_research.agent import AILeadResearchAgent
from app.lead_research.models import LeadDossier

logger = logging.getLogger(__name__)


def _email_hash(email: str) -> str:
    """Deterministic hash for email key."""
    import hashlib
    return hashlib.sha256(email.lower().strip().encode()).hexdigest()[:16]


class LeadResearchStore:
    """SQLite store for LeadDossier results."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            import os
            db_path = os.path.join(os.path.dirname(__file__), "..", "..", "output", "lead_research.db")
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        import os
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dossiers (
                email_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                domain TEXT NOT NULL,
                dossier_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def save(self, dossier: LeadDossier) -> None:
        """Save or update a dossier."""
        eh = _email_hash(dossier.email)
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            INSERT INTO dossiers (email_hash, email, domain, dossier_json, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(email_hash) DO UPDATE SET
                dossier_json = excluded.dossier_json,
                updated_at = CURRENT_TIMESTAMP
        """, (eh, dossier.email, dossier.domain, json.dumps(dossier.to_dict())))
        conn.commit()
        conn.close()

    def get(self, email: str) -> LeadDossier | None:
        """Retrieve a dossier by email."""
        eh = _email_hash(email)
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT dossier_json FROM dossiers WHERE email_hash = ?", (eh,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return LeadDossier.from_dict(json.loads(row[0]))

    def list_all(self) -> list[LeadDossier]:
        """List all stored dossiers."""
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            "SELECT dossier_json FROM dossiers ORDER BY updated_at DESC"
        ).fetchall()
        conn.close()
        return [LeadDossier.from_dict(json.loads(r[0])) for r in rows]

    def count(self) -> int:
        conn = sqlite3.connect(self._db_path)
        n = conn.execute("SELECT COUNT(*) FROM dossiers").fetchone()[0]
        conn.close()
        return n


class LeadResearchService:
    """High-level service: agent + store."""

    def __init__(
        self,
        *,
        store: LeadResearchStore | None = None,
        agent: AILeadResearchAgent | None = None,
    ) -> None:
        self.store = store or LeadResearchStore()
        self.agent = agent or AILeadResearchAgent()

    def research(self, email: str, domain: str) -> LeadDossier:
        """Research one email+domain and persist the result."""
        dossier = self.agent.research(email, domain)
        self.store.save(dossier)
        return dossier

    def get(self, email: str) -> LeadDossier | None:
        return self.store.get(email)

    def list_leads(self) -> list[LeadDossier]:
        return self.store.list_all()

    def research_batch(self, records: list[dict[str, str]]) -> list[LeadDossier]:
        """Research a batch of {email, domain} records."""
        results = []
        for rec in records:
            email = rec.get("email", "")
            domain = rec.get("domain", "")
            if not email or not domain:
                continue
            try:
                dossier = self.research(email, domain)
                results.append(dossier)
            except Exception as exc:
                logger.error("Batch research failed for %s: %s", email, exc)
        return results
