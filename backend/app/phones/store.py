"""PhoneLeadsStore — the Phones vertical's own ``phone_leads.db``.

Lives in its own SQLite file: phones are neither an auth concern (users.db),
research data (lead_research.db), nor outreach (campaigns.db) — they are a
separate product vertical (campaigns.db pattern, one DB per concern). Cross
-domain links are by value only.

Two tables:

* ``phone_leads`` — one row per (phone, business) pair harvested from a
  license-board source. ``trade`` is the canonical tradefold slug (P1) so
  the P2 strict-serve rule applies here too: a trade-filtered search only
  serves that trade's rows; other-trade rows are pool inventory for their
  own consumers.
* ``phone_lead_owners`` — the dossier_owners junction pattern (exclusivity
  at serve): serving a lead to a user stamps ownership, and a lead owned
  by another user NEVER serves to this one.

The store is pure persistence + queries; the search flow (pool-first serve,
live gap-fill) lives in service.py so it can be tested with a fake source.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from app.discovery.tradefold import normalize_trade

_INIT_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def normalize_phone(raw: str) -> str:
    """Digits-only US phone -> E.164 (``+1XXXXXXXXXX``), '' when unusable.

    License boards emit bare 10-digit strings ("5039573452") and occasional
    11-digit leading-1 forms; anything else (short, alpha, foreign-looking)
    is an honest drop, never a mangled lead.
    """
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


def pretty_person_name(raw: str) -> str:
    """License boards store person names LAST-FIRST (``GUERRERO MARTINEZ,
    CARLOS I.``); the UI wants ``Carlos I. Guerrero Martinez``.

    Title-casing applies ONLY to the flipped (comma) form — a real person
    name in ALL CAPS. Non-comma values (TDLR ``owner_name`` is often the
    BUSINESS name, "INFINITE POWER LLC") pass through untouched so legal
    suffixes stay intact.
    """
    raw = (raw or "").strip()
    if "," in raw:
        last, _, first = raw.partition(",")
        return f"{first.strip()} {last.strip()}".strip().title()
    return raw


class PhoneLeadsStore:
    """SQLite persistence + exclusive serve for phone leads."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "output", "phone_leads.db"
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
                CREATE TABLE IF NOT EXISTS phone_leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    person_name TEXT NOT NULL DEFAULT '',
                    business_name TEXT NOT NULL DEFAULT '',
                    trade TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    license_status TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (phone, business_name)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phone_lead_owners (
                    lead_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (lead_id, user_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_phone_leads_trade_state "
                "ON phone_leads (trade, state)"
            )
            conn.commit()
            conn.close()

    # -- write ---------------------------------------------------------------

    def add(self, records: list[dict[str, Any]]) -> dict[str, int]:
        """Stock harvested records into the pool.

        Every usable record is stocked (other-trade rows are inventory for
        their own consumers — the P2 banking rule), with the trade folded
        from the source label at ingest. Records with an unusable phone are
        honestly dropped and counted, never mangled in.

        Returns ``{"inserted": n, "duplicate": n, "dropped_bad_phone": n}``.
        """
        inserted = duplicate = dropped = 0
        conn = self._conn()
        try:
            for rec in records:
                phone = normalize_phone(rec.get("phone", ""))
                if not phone:
                    dropped += 1
                    continue
                trade = normalize_trade(rec.get("trade_category", "")) or \
                    normalize_trade(rec.get("business_name", ""))
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO phone_leads
                        (phone, person_name, business_name, trade, city, state,
                         source, license_status, source_url, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        phone,
                        pretty_person_name(rec.get("person_name", "")),
                        (rec.get("business_name", "") or "").strip(),
                        trade,
                        (rec.get("city", "") or "").strip().upper(),
                        (rec.get("state", "") or "").strip().upper()[:2],
                        rec.get("source", "") or "",
                        rec.get("license_status", "") or "",
                        rec.get("source_url", "") or "",
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
        return {
            "inserted": inserted,
            "duplicate": duplicate,
            "dropped_bad_phone": dropped,
        }

    # -- serve -----------------------------------------------------------------

    def _serve_clauses(
        self, trade: str, state: str, city: str,
    ) -> tuple[str, list[Any]]:
        """AND-fragment for a serve query's existing WHERE (P2 strict gate,
        fail-open on '')."""
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
        exclude_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Claim up to ``limit`` unowned-or-mine leads and stamp ownership.

        Exclusivity at serve (the dossier_owners rule): a lead already owned
        by ANOTHER user never serves here — the pool stays shared but each
        lead is one user's inventory once claimed.

        ``exclude_ids`` keeps one run's own earlier serves from re-serving
        the same row (the gap-fill re-serve after a pool serve).
        """
        if limit <= 0:
            return []
        frag, args = self._serve_clauses(trade, state, city)
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            frag += f" AND l.id NOT IN ({placeholders})"
            args = args + list(exclude_ids)
        conn = self._conn()
        try:
            cur = conn.execute(
                f"""
                SELECT l.* FROM phone_leads l
                WHERE l.id NOT IN (
                    SELECT lead_id FROM phone_lead_owners
                    WHERE user_id != ?
                ){frag}
                ORDER BY l.id ASC
                LIMIT ?
                """,
                [user_id] + args + [limit],
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            leads = [dict(zip(cols, r)) for r in rows]
            ts = _now()
            for lead in leads:
                conn.execute(
                    "INSERT OR IGNORE INTO phone_lead_owners "
                    "(lead_id, user_id, created_at) VALUES (?, ?, ?)",
                    (lead["id"], user_id, ts),
                )
            conn.commit()
            return leads
        finally:
            conn.close()

    def unclaimed_count(self, trade: str, state: str = "", city: str = "") -> int:
        """How many servable (unowned) rows the pool holds for this filter —
        the number a gap-fill does NOT need to fetch live."""
        frag, args = self._serve_clauses(trade, state, city)
        conn = self._conn()
        try:
            row = conn.execute(
                f"""
                SELECT COUNT(*) FROM phone_leads l
                WHERE l.id NOT IN (
                    SELECT lead_id FROM phone_lead_owners
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
        """The user's own phone leads (claimed by serve), newest-served first."""
        frag, args = self._serve_clauses(trade, state, city)
        conn = self._conn()
        try:
            cur = conn.execute(
                f"""
                SELECT l.*, o.created_at AS claimed_at FROM phone_leads l
                JOIN phone_lead_owners o ON o.lead_id = l.id
                AND o.user_id = ?{frag}
                ORDER BY o.created_at DESC, l.id DESC
                LIMIT ?
                """,
                [user_id] + args + [limit],
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    def pool_stats(self) -> dict[str, Any]:
        """Honest pool inventory: totals + per-trade counts (admin/diagnostic)."""
        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM phone_leads").fetchone()[0]
            claimed = conn.execute(
                "SELECT COUNT(DISTINCT lead_id) FROM phone_lead_owners"
            ).fetchone()[0]
            by_trade = {
                r[0] or "(unknown)": r[1]
                for r in conn.execute(
                    "SELECT trade, COUNT(*) FROM phone_leads GROUP BY trade "
                    "ORDER BY COUNT(*) DESC"
                ).fetchall()
            }
            by_state = {
                r[0] or "(unknown)": r[1]
                for r in conn.execute(
                    "SELECT state, COUNT(*) FROM phone_leads GROUP BY state "
                    "ORDER BY COUNT(*) DESC LIMIT 20"
                ).fetchall()
            }
            return {
                "total": total,
                "claimed": claimed,
                "unclaimed": total - claimed,
                "by_trade": by_trade,
                "by_state": by_state,
            }
        finally:
            conn.close()
