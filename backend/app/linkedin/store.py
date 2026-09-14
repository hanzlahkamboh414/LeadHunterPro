"""LinkedInLeadsStore — the LinkedIn vertical's own ``linkedin_leads.db``.

One SQLite file per concern (the campaigns.db / phone_leads.db pattern):
LinkedIn leads are neither emails (lead_research.db) nor phones
(phone_leads.db) — they are the THIRD vertical's inventory, and like phones
they link to the other domains by VALUE only.

Where the inventory comes from — the byproduct rule: every researched
dossier whose person carries an honest ``linkedin.com/in/…`` URL stocks a
row here (:meth:`add`, called from LeadResearchStore.save). There is NO
live LinkedIn fetch and no quota: the vertical serves what email research
has already produced, exclusively at serve (the dossier_owners /
phone_lead_owners junction pattern — a lead claimed by one user never
serves to another).
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from app.discovery.tradefold import normalize_trade

_INIT_LOCK = threading.RLock()

#: A PERSON profile URL is the only honest LinkedIn lead. Company pages,
#: school pages, raw linkedin.com roots and lookalike hosts are not stocked.
#: Extra path segments are allowed (the legacy ``/pub/name/1/2/3`` form and
#: ``/in/name/details`` style subpages are still the person's profile).
_LINKEDIN_PERSON_RE = re.compile(
    r"^https?://(www\.)?linkedin\.com/(in|pub)/[A-Za-z0-9._%-]+"
    r"(/[A-Za-z0-9._%-]+)*/?$",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def is_person_linkedin_url(raw: str) -> bool:
    """True only for a clean personal-profile URL (``/in/`` or ``/pub/``).

    Tracking query strings and fragments are stripped BEFORE the check, so
    ``linkedin.com/in/jane?trk=…`` is still honest — but the dedup key uses
    the clean form so the same profile never stocks twice.
    """
    url = (raw or "").strip().split("?")[0].split("#")[0]
    return bool(_LINKEDIN_PERSON_RE.match(url))


def clean_linkedin_url(raw: str) -> str:
    """The dedup-safe form: no query string, no fragment, no trailing slash
    variance that would split one profile into two rows."""
    url = (raw or "").strip().split("?")[0].split("#")[0]
    return url.rstrip("/")


def split_location(raw: str) -> tuple[str, str]:
    """``"Vancouver, WA"`` -> ``("Vancouver", "WA")``; anything unparsable is
    an honest ``("", "")`` — a guessed state would silently mis-filter."""
    parts = [p.strip() for p in (raw or "").split(",")]
    if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isalpha():
        return parts[0], parts[1].upper()
    return "", ""


class LinkedInLeadsStore:
    """SQLite persistence + exclusive serve for LinkedIn person leads."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "output",
                "linkedin_leads.db",
            )
        self._db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        with _INIT_LOCK:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            conn = self._conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS linkedin_leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    linkedin_url TEXT NOT NULL,
                    company_name TEXT NOT NULL DEFAULT '',
                    domain TEXT NOT NULL DEFAULT '',
                    trade TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'email_research',
                    source_email TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (linkedin_url)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS linkedin_lead_owners (
                    lead_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (lead_id, user_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_linkedin_leads_trade_state "
                "ON linkedin_leads (trade, state)"
            )
            conn.commit()
            conn.close()

    # -- write ---------------------------------------------------------------

    def add(self, records: list[dict[str, Any]]) -> dict[str, int]:
        """Stock byproduct rows; returns ``{"inserted": n, "duplicate": n,
        "dropped": n}`` — ``dropped`` counts non-person URLs (honest refuse,
        never a company page dressed up as a person lead)."""
        inserted = duplicate = dropped = 0
        conn = self._conn()
        try:
            for rec in records:
                url = clean_linkedin_url(rec.get("linkedin_url", ""))
                if not is_person_linkedin_url(url):
                    dropped += 1
                    continue
                city, state = split_location(rec.get("location", ""))
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO linkedin_leads
                        (person_name, role, linkedin_url, company_name,
                         domain, trade, city, state, source, source_email,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (rec.get("person_name", "") or "").strip(),
                        (rec.get("role", "") or "").strip(),
                        url,
                        (rec.get("company_name", "") or "").strip(),
                        (rec.get("domain", "") or "").strip(),
                        normalize_trade(rec.get("trade", "")),
                        city,
                        state,
                        rec.get("source", "") or "email_research",
                        rec.get("source_email", "") or "",
                        _now(), _now(),
                    ),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    duplicate += 1
            conn.commit()
        finally:
            conn.close()
        return {"inserted": inserted, "duplicate": duplicate,
                "dropped": dropped}

    # -- serve -----------------------------------------------------------------

    def _serve_clauses(
        self, trade: str, state: str, city: str,
    ) -> tuple[str, list[Any]]:
        """AND-fragment for the serve filters (P2 strict gate, fail-open on
        ''). Same shape as PhoneLeadsStore."""
        clauses: list[str] = []
        args: list[Any] = []
        if trade:
            clauses.append("l.trade = ?")
            args.append(trade)
        if state:
            clauses.append("l.state = ?")
            args.append(state.upper())
        if city:
            clauses.append("l.city LIKE ?")
            args.append(f"%{city.strip().upper()}%")
        frag = (" AND " + " AND ".join(clauses)) if clauses else ""
        return frag, args

    def serve(
        self, trade: str, state: str, city: str, limit: int, user_id: str,
    ) -> list[dict[str, Any]]:
        """Claim up to ``limit`` unowned-or-mine leads and stamp ownership
        (exclusivity at serve — the dossier_owners rule)."""
        if limit <= 0:
            return []
        frag, args = self._serve_clauses(trade, state, city)
        conn = self._conn()
        try:
            cur = conn.execute(
                f"""
                SELECT l.* FROM linkedin_leads l
                WHERE l.id NOT IN (
                    SELECT lead_id FROM linkedin_lead_owners
                    WHERE user_id != ?
                ){frag}
                ORDER BY l.id ASC
                LIMIT ?
                """,
                [user_id, *args, limit],
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            leads = [dict(zip(cols, r, strict=True)) for r in rows]
            ts = _now()
            for lead in leads:
                conn.execute(
                    "INSERT OR IGNORE INTO linkedin_lead_owners "
                    "(lead_id, user_id, created_at) VALUES (?, ?, ?)",
                    (lead["id"], user_id, ts),
                )
            conn.commit()
            return leads
        finally:
            conn.close()

    def unclaimed_count(self, trade: str, state: str = "", city: str = "") -> int:
        frag, args = self._serve_clauses(trade, state, city)
        conn = self._conn()
        try:
            row = conn.execute(
                f"""
                SELECT COUNT(*) FROM linkedin_leads l
                WHERE l.id NOT IN (
                    SELECT lead_id FROM linkedin_lead_owners
                ){frag}
                """,
                args,
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    # -- read ------------------------------------------------------------------

    def list_owned(
        self, user_id: str, trade: str = "", state: str = "", city: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """The user's own LinkedIn leads (claimed at serve), newest first."""
        frag, args = self._serve_clauses(trade, state, city)
        conn = self._conn()
        try:
            cur = conn.execute(
                f"""
                SELECT l.*, o.created_at AS claimed_at FROM linkedin_leads l
                JOIN linkedin_lead_owners o ON o.lead_id = l.id
                AND o.user_id = ?{frag}
                ORDER BY o.created_at DESC, l.id DESC
                LIMIT ?
                """,
                [user_id, *args, limit],
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in rows]
        finally:
            conn.close()

    def pool_stats(self) -> dict[str, Any]:
        """Honest pool inventory (diagnostic/admin)."""
        conn = self._conn()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM linkedin_leads"
            ).fetchone()[0]
            claimed = conn.execute(
                "SELECT COUNT(DISTINCT lead_id) FROM linkedin_lead_owners"
            ).fetchone()[0]
            by_trade = {
                r[0] or "(unknown)": r[1]
                for r in conn.execute(
                    "SELECT trade, COUNT(*) FROM linkedin_leads GROUP BY trade "
                    "ORDER BY COUNT(*) DESC"
                ).fetchall()
            }
            return {
                "total": total,
                "claimed": claimed,
                "unclaimed": total - claimed,
                "by_trade": by_trade,
            }
        finally:
            conn.close()


_store: LinkedInLeadsStore | None = None
_store_lock = threading.Lock()


def get_store() -> LinkedInLeadsStore:
    """Process-wide singleton bound to the real output DB. Tests monkeypatch
    this — construction is lazy so importing the module never creates the
    file."""
    global _store
    with _store_lock:
        if _store is None:
            _store = LinkedInLeadsStore()
        return _store
