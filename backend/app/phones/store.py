"""PhoneLeadsStore — the Phones vertical's own ``phone_leads.db``.

Lives in its own SQLite file: phones are neither an auth concern (users.db),
research data (lead_research.db), nor outreach (campaigns.db) — they are a
separate product vertical (campaigns.db pattern, one DB per concern). Cross
-domain links are by value only.

Tables:

* ``phone_leads`` — one row per (phone, business) pair harvested from a
  license-board source. ``trade`` is the canonical tradefold slug (P1) so
  the P2 strict-serve rule applies here too: a trade-filtered search only
  serves that trade's rows; other-trade rows are pool inventory for their
  own consumers. P7.5 adds the voicemail columns — ``voicemail_count`` /
  ``voicemail_at`` drive the tiered recycling (see VOICEMAIL_COOLDOWN_DAYS).
* ``phone_lead_owners`` — the dossier_owners junction pattern (exclusivity
  at serve): serving a lead to a user stamps ownership, and a lead owned
  by another user NEVER serves to this one. A voicemail RELEASES the row
  back to the shared rotation (after its cooldown the number serves again
  — data reuse across users).
* ``phone_user_leads`` — the calling workflow's SAVED output (P7.5): a
  ✓Lead (the person promised a project) or a 💾Store contact, each a
  full snapshot keyed by (user_id, phone, kind) so it SURVIVES the pool
  row's retirement, plus the user's own 📝note.
* ``phone_suppressions`` — the deleted_emails pattern for phones: numbers
  retired from the pool (claimed as a lead, or a 4th voicemail) are never
  re-added by a later harvest.

The store is pure persistence + queries; the search flow (pool-first serve)
lives in service.py so it can be tested with a fake source.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from app.discovery.tradefold import normalize_trade

_INIT_LOCK = threading.RLock()

#: Tiered voicemail recycling (P7.5, the user's approved ladder): the Nth
#: voicemail parks the number for that many days, then it re-enters the
#: shared rotation. A FOURTH voicemail retires the number for good — the
#: row leaves phone_leads and the number lands in phone_suppressions so a
#: later harvest can never re-add it (the deleted_emails pattern).
VOICEMAIL_COOLDOWN_DAYS: dict[int, int] = {1: 14, 2: 30, 3: 60}
MAX_VOICEMAILS = 4


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
                    email TEXT NOT NULL DEFAULT '',
                    email_source TEXT NOT NULL DEFAULT '',
                    website TEXT NOT NULL DEFAULT '',
                    enriched_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (phone, business_name)
                )
            """)
            # Pre-enrichment databases: the phone_enrichment columns arrive as
            # an additive ALTER (the users.category pattern) — a phone_leads.db
            # created before this phase keeps every row, all with email=''.
            existing = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(phone_leads)"
                ).fetchall()
            }
            for col, decl in (
                ("email", "TEXT NOT NULL DEFAULT ''"),
                ("email_source", "TEXT NOT NULL DEFAULT ''"),
                ("website", "TEXT NOT NULL DEFAULT ''"),
                ("enriched_at", "TEXT NOT NULL DEFAULT ''"),
                # P7.5 tiered voicemail recycling (additive ALTER, same
                # pattern): a parked number's count + last-voicemail time.
                ("voicemail_count", "INTEGER NOT NULL DEFAULT 0"),
                ("voicemail_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in existing:
                    conn.execute(
                        f"ALTER TABLE phone_leads ADD COLUMN {col} {decl}"
                    )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phone_lead_owners (
                    lead_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (lead_id, user_id)
                )
            """)
            # P7.5 — the calling workflow's saved output. Snapshot keyed by
            # (user_id, phone, kind) so it survives the pool row's retirement.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phone_user_leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    person_name TEXT NOT NULL DEFAULT '',
                    business_name TEXT NOT NULL DEFAULT '',
                    trade TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    license_status TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    email_source TEXT NOT NULL DEFAULT '',
                    website TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'contact',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (user_id, phone, kind)
                )
            """)
            # P7.5 — the suppression cache: numbers retired from the pool are
            # never re-added by a later harvest (the deleted_emails pattern).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phone_suppressions (
                    phone TEXT PRIMARY KEY,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
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
        honestly dropped and counted, never mangled in. SUPPRESSED numbers
        (retired as someone's lead, or a 4th-voicemail retire) are skipped
        and counted — a retired number never re-enters the pool no matter
        how many times the harvester re-fetches its page (P7.5).

        Returns ``{"inserted": n, "duplicate": n, "dropped_bad_phone": n,
        "suppressed": n}``.
        """
        inserted = duplicate = dropped = suppressed = 0
        conn = self._conn()
        try:
            banned = {
                r[0] for r in conn.execute(
                    "SELECT phone FROM phone_suppressions"
                ).fetchall()
            }
            for rec in records:
                phone = normalize_phone(rec.get("phone", ""))
                if not phone:
                    dropped += 1
                    continue
                if phone in banned:
                    suppressed += 1
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
            "suppressed": suppressed,
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

    @staticmethod
    def _resting_frag() -> str:
        """AND-fragment excluding VOICEMAIL-PARKED rows: a number whose Nth
        voicemail is still inside its tier cooldown is resting — it serves
        to NOBODY until the window passes, then re-enters the shared
        rotation (P7.5 recycling). SQLite parses the ISO 'T' timestamps
        ``_now()`` writes."""
        conds = " OR ".join(
            f"(l.voicemail_count = {n} "
            f"AND l.voicemail_at > datetime('now', '-{days} days'))"
            for n, days in VOICEMAIL_COOLDOWN_DAYS.items()
        )
        return f" AND NOT ({conds})"

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
        frag += self._resting_frag()
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
                [user_id, *args, limit],
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            leads = [dict(zip(cols, r, strict=True)) for r in rows]
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
        frag += self._resting_frag()
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

    # -- enrichment -----------------------------------------------------------

    def pending_enrichment(
        self, limit: int, claimed_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Leads the enrichment worker should work on next.

        CLAIMED FIRST (``claimed_only`` default): a lead nobody owns has no
        user waiting on its email — enrichment effort goes where someone is
        looking. Unclaimed rows are picked up by a later pass once served.
        A lead with ``enriched_at`` set is done (found OR honestly none) and
        never re-enriched: one attempt per lead, no retry loop.
        """
        frag = " AND l.id IN (SELECT lead_id FROM phone_lead_owners)" \
            if claimed_only else ""
        conn = self._conn()
        try:
            cur = conn.execute(
                f"""
                SELECT l.* FROM phone_leads l
                WHERE l.enriched_at = ''{frag}
                ORDER BY l.id ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in rows]
        finally:
            conn.close()

    def set_enrichment(
        self, lead_id: int, *, email: str, email_source: str, website: str,
    ) -> None:
        """Record one lead's enrichment outcome — a FOUND email, or the honest
        'tried and none findable' empty string. Either way ``enriched_at``
        stamps the lead done so the worker never re-attempts it."""
        conn = self._conn()
        try:
            conn.execute(
                """
                UPDATE phone_leads
                SET email = ?, email_source = ?, website = ?,
                    enriched_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (email.strip(), email_source.strip(), website.strip(),
                 _now(), _now(), lead_id),
            )
            conn.commit()
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
                [user_id, *args, limit],
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in rows]
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

    # -- calling workflow (P7.5) ----------------------------------------------

    def _owned_lead(self, lead_id: int, user_id: str) -> dict[str, Any] | None:
        """The lead row when ``user_id`` owns it (claimed at serve), else
        None. Every calling-workflow action is owner-only: a number another
        user owns is not on this user's sheet, so there is nothing to act
        on."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM phone_lead_owners "
                "WHERE lead_id = ? AND user_id = ?",
                (lead_id, user_id),
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                "SELECT * FROM phone_leads WHERE id = ?", (lead_id,)
            )
            cols = [d[0] for d in cur.description]
            r = cur.fetchone()
            return dict(zip(cols, r, strict=True)) if r else None
        finally:
            conn.close()

    def _retire(self, lead_id: int, phone: str, reason: str) -> None:
        """Remove a lead from the pool FOR GOOD and suppress the number —
        the row, its owner stamps, everything except the users' SAVED
        snapshots (those live in phone_user_leads keyed by phone). A later
        harvest can never re-add the number (phone_suppressions)."""
        conn = self._conn()
        try:
            conn.execute("DELETE FROM phone_leads WHERE id = ?", (lead_id,))
            conn.execute(
                "DELETE FROM phone_lead_owners WHERE lead_id = ?", (lead_id,)
            )
            conn.execute(
                "INSERT OR IGNORE INTO phone_suppressions (phone, reason, "
                "created_at) VALUES (?, ?, ?)",
                (phone, reason, _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def _save_snapshot(
        self, lead: dict[str, Any], user_id: str, kind: str, note: str = "",
    ) -> int:
        """Insert-or-refresh the user's saved copy of a lead (kind 'lead' or
        'contact'). The snapshot is keyed by (user_id, phone, kind) so it
        survives the pool row's retirement; a re-save refreshes the fields
        (enrichment may have found the email since the first save)."""
        ts = _now()
        conn = self._conn()
        try:
            cur = conn.execute(
                """
                INSERT INTO phone_user_leads
                    (user_id, phone, person_name, business_name, trade, city,
                     state, source, source_url, license_status, email,
                     email_source, website, kind, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, phone, kind) DO UPDATE SET
                    person_name = excluded.person_name,
                    business_name = excluded.business_name,
                    trade = excluded.trade,
                    city = excluded.city,
                    state = excluded.state,
                    source = excluded.source,
                    source_url = excluded.source_url,
                    license_status = excluded.license_status,
                    email = excluded.email,
                    email_source = excluded.email_source,
                    website = excluded.website,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id, lead["phone"], lead.get("person_name", ""),
                    lead.get("business_name", ""), lead.get("trade", ""),
                    lead.get("city", ""), lead.get("state", ""),
                    lead.get("source", ""), lead.get("source_url", ""),
                    lead.get("license_status", ""), lead.get("email", ""),
                    lead.get("email_source", ""), lead.get("website", ""),
                    kind, note, ts, ts,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM phone_user_leads WHERE user_id = ? "
                "AND phone = ? AND kind = ?",
                (user_id, lead["phone"], kind),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def mark_lead(self, lead_id: int, user_id: str) -> dict[str, Any] | None:
        """✓Lead: the person said "project doonga" on the call — the number
        becomes THIS user's lead permanently. The snapshot saves to their
        account, the pool row RETIRES (a lead number is never served to
        another user), and the number is suppressed against re-harvest.
        None when the user doesn't own the lead."""
        lead = self._owned_lead(lead_id, user_id)
        if lead is None:
            return None
        saved_id = self._save_snapshot(lead, user_id, kind="lead")
        self._retire(lead_id, lead["phone"], "claimed_lead")
        return {"saved_id": saved_id, "retired": True}

    def store_contact(self, lead_id: int, user_id: str) -> dict[str, Any] | None:
        """💾Store: keep the contact in the account (not a lead — just a
        number worth keeping). The row STAYS claimed: it remains on the
        user's call sheet and never serves to anyone else while claimed."""
        lead = self._owned_lead(lead_id, user_id)
        if lead is None:
            return None
        saved_id = self._save_snapshot(lead, user_id, kind="contact")
        return {"saved_id": saved_id, "retired": False}

    def mark_voicemail(self, lead_id: int, user_id: str) -> dict[str, Any] | None:
        """☎Voicemail: nobody answered — park the number and move on.

        Tiered recycling (the user's approved ladder): the Nth voicemail
        parks the number for 14/30/60 days, then it re-enters the shared
        rotation for the next caller (data reuse — "aaj voice mail pr ha to
        shayad 1 2 mah bad na ho"). The user's claim is RELEASED (the row
        leaves their sheet — it is not theirs to keep if they won't talk to
        it). A FOURTH voicemail retires the number for good: row deleted,
        number suppressed, harvester can never re-add it. None when the
        user doesn't own the lead."""
        lead = self._owned_lead(lead_id, user_id)
        if lead is None:
            return None
        count = int(lead.get("voicemail_count") or 0) + 1
        if count >= MAX_VOICEMAILS:
            self._retire(lead_id, lead["phone"], "voicemail_retired")
            return {
                "retired": True, "voicemail_count": count, "cooldown_days": 0,
            }
        cooldown = VOICEMAIL_COOLDOWN_DAYS.get(count, 60)
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE phone_leads SET voicemail_count = ?, "
                "voicemail_at = ?, updated_at = ? WHERE id = ?",
                (count, _now(), _now(), lead_id),
            )
            # Release the claim: the row rests for the cooldown, then serves
            # again from the shared rotation (to this user or any other).
            conn.execute(
                "DELETE FROM phone_lead_owners WHERE lead_id = ?", (lead_id,)
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "retired": False, "voicemail_count": count,
            "cooldown_days": cooldown,
        }

    def note_lead(
        self, lead_id: int, user_id: str, note: str,
    ) -> dict[str, Any] | None:
        """📝Note on a lead still on the call sheet: the note saves to a
        contact-kind snapshot (auto-stored — writing a note IS keeping it).
        None when the user doesn't own the lead."""
        lead = self._owned_lead(lead_id, user_id)
        if lead is None:
            return None
        saved_id = self._save_snapshot(lead, user_id, kind="contact")
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE phone_user_leads SET note = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (note.strip(), _now(), saved_id, user_id),
            )
            conn.commit()
        finally:
            conn.close()
        return {"saved_id": saved_id, "retired": False}

    def set_saved_note(self, saved_id: int, user_id: str, note: str) -> bool:
        """Edit the note on an already-saved lead/contact row."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "UPDATE phone_user_leads SET note = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (note.strip(), _now(), saved_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def list_saved(
        self, user_id: str, kind: str = "", limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """The user's saved leads/contacts (✓Lead + 💾Store output),
        newest-saved first. ``kind`` filters ('lead' | 'contact'); '' = all."""
        frag = " AND kind = ?" if kind else ""
        args: list[Any] = [user_id] + ([kind] if kind else []) + [limit]
        conn = self._conn()
        try:
            cur = conn.execute(
                f"""
                SELECT * FROM phone_user_leads
                WHERE user_id = ?{frag}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                args,
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in rows]
        finally:
            conn.close()

    def delete_saved(self, saved_id: int, user_id: str) -> bool:
        """Delete one of the user's saved leads/contacts (their account,
        their decision — the pool row, if any, is untouched by this)."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "DELETE FROM phone_user_leads WHERE id = ? AND user_id = ?",
                (saved_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
