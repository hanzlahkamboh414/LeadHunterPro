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
* ``phone_lead_owners`` — the dossier_owners junction pattern, now a
  ONE-SHOT serve (user's policy, 2026-09-16): serving a lead stamps
  ownership, and an owned lead NEVER serves again — not to another user,
  not back to the user who already has it. A repeat search of a state
  therefore returns only fresh numbers. A voicemail RELEASES the row (the
  ownership row is deleted) so it can rest out its cooldown and re-enter
  the shared rotation — that release is explicit and is the only path back
  in besides a new harvest. Each serve call stamps ``batch_at`` for legacy
  diagnostics; the active call sheet now shows all of today's still-owned
  claims across searches and states (see ``list_owned``).
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
import json
import sqlite3
import threading
import uuid
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
DEFAULT_DAILY_PHONE_LIMIT = 600


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _new_batch() -> str:
    """One serve call's batch stamp.

    The call sheet shows the LATEST batch only (the user's rule of
    2026-09-16): every search replaces the sheet, the numbers behind it go
    quiet but stay owned. The stamp must therefore never repeat across two
    calls — two searches in the same second are normal, so the second clock
    alone is not enough. Microseconds plus a short random suffix makes it
    unique, and the ISO prefix keeps it lexicographically ordered so MAX()
    is genuinely the newest batch.
    """
    return (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
            + "#" + uuid.uuid4().hex[:8])


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
                ("trade_evidence_url", "TEXT NOT NULL DEFAULT ''"),
                ("trade_evidence_kind", "TEXT NOT NULL DEFAULT ''"),
                ("trade_evidence_ref", "TEXT NOT NULL DEFAULT ''"),
                ("trade_checked_at", "TEXT NOT NULL DEFAULT ''"),
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
                    batch_at TEXT NOT NULL DEFAULT '',
                    UNIQUE (lead_id, user_id)
                )
            """)
            # The call sheet shows the user's latest serve batch only. Rows
            # claimed BEFORE this column existed keep batch_at='' — an empty
            # stamp is never a batch, so those claims are hidden from the
            # sheet from the first deploy onwards (the user's "kal ka data
            # hide kar do": the sheet starts clean and the next search fills
            # it). Their ownership is untouched — the numbers still serve to
            # nobody.
            owner_cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(phone_lead_owners)"
                ).fetchall()
            }
            if "batch_at" not in owner_cols:
                conn.execute(
                    "ALTER TABLE phone_lead_owners ADD COLUMN batch_at "
                    "TEXT NOT NULL DEFAULT ''"
                )
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
            events_exist = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'phone_claim_events'"
            ).fetchone() is not None
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phone_claim_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            if not events_exist:
                conn.execute("""
                    INSERT INTO phone_claim_events
                        (lead_id, user_id, phone, created_at)
                    SELECT o.lead_id, o.user_id, l.phone, o.created_at
                    FROM phone_lead_owners o JOIN phone_leads l ON l.id = o.lead_id
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phone_daily_limits (
                    user_id TEXT PRIMARY KEY,
                    daily_limit INTEGER NOT NULL CHECK (daily_limit >= 0)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phone_call_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    lead_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    person_name TEXT NOT NULL DEFAULT '',
                    business_name TEXT NOT NULL DEFAULT '',
                    trade TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phone_wrong_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    recovered_at TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_phone_call_day "
                "ON phone_call_events (user_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_phone_claim_day "
                "ON phone_claim_events (user_id, created_at)"
            )
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
                # A board with no trade column is eligible for state-only
                # searches; its business name is not classification evidence.
                # A business name is an identity, not proof of its trade.
                # Explicit state-only feeds and sources without a trade label
                # remain in raw stock until a separate evidence check passes.
                trade = "" if rec.get("state_only") is True else \
                    normalize_trade(rec.get("trade_category", ""))
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
    def _qualified_frag() -> str:
        """Raw inventory is never a new phone claim or an availability count."""
        return " AND TRIM(l.business_name) <> '' AND TRIM(l.trade) <> ''"

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

    def daily_limit(self, user_id: str) -> int:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT daily_limit FROM phone_daily_limits WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return int(row[0]) if row else DEFAULT_DAILY_PHONE_LIMIT
        finally:
            conn.close()

    def set_daily_limit(self, user_id: str, limit: int) -> None:
        if limit < 0 or limit > 5000:
            raise ValueError("daily phone limit must be between 0 and 5000")
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO phone_daily_limits (user_id, daily_limit) "
                "VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "daily_limit = excluded.daily_limit",
                (user_id, limit),
            )
            conn.commit()
        finally:
            conn.close()

    def daily_usage(self, user_id: str) -> int:
        conn = self._conn()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM phone_claim_events WHERE user_id = ? "
                "AND substr(created_at, 1, 10) = ?",
                (user_id, _now()[:10]),
            ).fetchone()[0])
        finally:
            conn.close()

    def daily_remaining(self, user_id: str) -> int:
        return max(0, self.daily_limit(user_id) - self.daily_usage(user_id))

    def serve(
        self, trade: str, state: str, city: str, limit: int, user_id: str,
        exclude_ids: list[int] | None = None,
        enforce_quota: bool = True,
    ) -> list[dict[str, Any]]:
        """Claim up to ``limit`` UNOWNED leads and stamp ownership.

        One-shot serve, by the user's policy of 2026-09-16: a lead with an
        ownership row serves to NOBODY — not to another user (the original
        dossier_owners exclusivity) and not back to the user who already
        claimed it. A repeat search of the same state therefore returns only
        FRESH numbers; the already-served ones go quiet — reachable by no one.

        Every row claimed by one call shares a ``batch_at``, and the call
        sheet shows the latest batch only: the numbers a search hands over
        are exactly what the user sees, and the previous batch leaves the
        screen (still owned, still exclusive — ``list_owned``).

        Two paths put a number back into the shared rotation, and both are
        explicit: a voicemail RELEASES the ownership row (then the row rests
        out its tier cooldown), and a ✓Lead retires the number for good into
        ``phone_suppressions``. Nothing else un-claims a lead.

        ``exclude_ids`` keeps one run's own earlier serves from re-serving
        the same row (the gap-fill re-serve after a pool serve).
        """
        if limit <= 0:
            return []
        frag, args = self._serve_clauses(trade, state, city)
        frag += self._qualified_frag()
        frag += self._resting_frag()
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            frag += f" AND l.id NOT IN ({placeholders})"
            args = args + list(exclude_ids)
        conn = self._conn()
        try:
            # Lock before counting and claiming: concurrent searches for the
            # same account cannot both spend the same remaining allowance.
            conn.execute("BEGIN IMMEDIATE")
            if enforce_quota:
                limit_row = conn.execute(
                    "SELECT daily_limit FROM phone_daily_limits WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                allowance = int(limit_row[0]) if limit_row else DEFAULT_DAILY_PHONE_LIMIT
                used = int(conn.execute(
                    "SELECT COUNT(*) FROM phone_claim_events WHERE user_id = ? "
                    "AND substr(created_at, 1, 10) = ?",
                    (user_id, _now()[:10]),
                ).fetchone()[0])
                limit = min(limit, max(0, allowance - used))
            if limit <= 0:
                conn.rollback()
                return []
            cur = conn.execute(
                f"""
                SELECT l.* FROM phone_leads l
                WHERE l.id NOT IN (
                    SELECT lead_id FROM phone_lead_owners
                ){frag}
                ORDER BY l.id ASC
                LIMIT ?
                """,
                [*args, limit],
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            leads = [dict(zip(cols, r, strict=True)) for r in rows]
            ts = _now()
            # Every row this call claims carries the SAME batch stamp: the
            # sheet reads the latest batch, so one search = one sheet.
            batch = _new_batch()
            for lead in leads:
                claimed = conn.execute(
                    "INSERT OR IGNORE INTO phone_lead_owners "
                    "(lead_id, user_id, created_at, batch_at) "
                    "VALUES (?, ?, ?, ?)",
                    (lead["id"], user_id, ts, batch),
                )
                if claimed.rowcount:
                    conn.execute(
                        "INSERT INTO phone_claim_events "
                        "(lead_id, user_id, phone, created_at) VALUES (?, ?, ?, ?)",
                        (lead["id"], user_id, lead["phone"], ts),
                    )
            conn.commit()
            return leads
        finally:
            conn.close()

    def unclaimed_count(self, trade: str, state: str = "", city: str = "") -> int:
        """How many servable (unowned) rows the pool holds for this filter —
        the number a gap-fill does NOT need to fetch live."""
        frag, args = self._serve_clauses(trade, state, city)
        frag += self._qualified_frag()
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

    def unqualified_count(self, state: str = "", city: str = "") -> int:
        """Raw unowned businesses withheld from serve pending trade proof."""
        clauses = ["TRIM(l.business_name) <> ''", "TRIM(l.trade) = ''"]
        args: list[Any] = []
        if state:
            clauses.append("l.state = ?")
            args.append(state.strip().upper()[:2])
        if city:
            clauses.append("l.city LIKE ?")
            args.append(f"%{city.strip().upper()}%")
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM phone_leads l WHERE "
                + " AND ".join(clauses)
                + " AND l.id NOT IN (SELECT lead_id FROM phone_lead_owners)",
                args,
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def servable_by_state(self) -> dict[str, int]:
        """Per-STATE count of rows that would serve right now — unclaimed
        (no ownership row) and not voicemail-parked.

        This is the honest "is location mein kitna naya data hai" number the
        Phones screen shows next to each location: a state whose numbers were
        already handed out reads 0 even though the pool file still holds them
        (one-shot serve), because those rows belong to somebody's call sheet
        and serve to no one. States the pool has never stocked are absent from
        the map (the caller reads them as 0 — never as "unknown")."""
        conn = self._conn()
        try:
            rows = conn.execute(
                f"""
                SELECT l.state, COUNT(*) FROM phone_leads l
                WHERE l.id NOT IN (
                    SELECT lead_id FROM phone_lead_owners
                ){self._qualified_frag()}{self._resting_frag()}
                GROUP BY l.state
                ORDER BY COUNT(*) DESC
                """
            ).fetchall()
            return {(r[0] or "").upper(): int(r[1]) for r in rows}
        finally:
            conn.close()

    # -- enrichment -----------------------------------------------------------

    def pending_trade_enrichment(self, limit: int) -> list[dict[str, Any]]:
        """Raw businesses due for a trade evidence check, including unclaimed.

        An honest miss is retried after seven days, rather than marked as a
        permanent failure. Unclaimed stock goes first because it can still
        become a fresh qualified lead for a user.
        """
        if limit <= 0:
            return []
        conn = self._conn()
        try:
            cur = conn.execute(
                """
                SELECT l.* FROM phone_leads l
                WHERE TRIM(l.business_name) <> '' AND TRIM(l.trade) = ''
                  AND (l.trade_checked_at = '' OR
                       datetime(l.trade_checked_at) <=
                       datetime('now', '-7 days'))
                ORDER BY EXISTS (
                    SELECT 1 FROM phone_lead_owners o WHERE o.lead_id = l.id
                ), l.id ASC
                LIMIT ?
                """, (limit,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in rows]
        finally:
            conn.close()

    def raw_lead_id(self, *, source: str, phone: str,
                    business_name: str) -> int | None:
        """Find the exact raw pool row for an official-license join."""
        normalized = normalize_phone(phone)
        if not normalized or not business_name.strip():
            return None
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id FROM phone_leads WHERE source = ? AND phone = ? "
                "AND business_name = ? AND TRIM(trade) = '' LIMIT 1",
                (source, normalized, business_name.strip()),
            ).fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()

    def set_trade_resolution(
        self, lead_id: int, *, trade: str, evidence_url: str,
        evidence_kind: str, evidence_ref: str = "",
    ) -> bool:
        """Promote only a canonical, cited trade; retain honest misses raw."""
        from urllib.parse import urlparse

        slug = normalize_trade(trade)
        url = evidence_url.strip()
        parsed = urlparse(url)
        valid_evidence = (parsed.scheme in ("http", "https") and
                          bool(parsed.netloc) and
                          evidence_kind in ("company_website", "official_license"))
        if trade and (not slug or not valid_evidence):
            return False
        if not trade and (url or evidence_kind or evidence_ref):
            return False
        conn = self._conn()
        try:
            ts = _now()
            cur = conn.execute(
                """
                UPDATE phone_leads SET trade = ?, trade_evidence_url = ?,
                    trade_evidence_kind = ?, trade_evidence_ref = ?,
                    trade_checked_at = ?,
                    updated_at = ?
                WHERE id = ? AND TRIM(business_name) <> '' AND TRIM(trade) = ''
                """, (slug, url, evidence_kind, evidence_ref.strip(),
                      ts, ts, lead_id))
            conn.commit()
            return bool(cur.rowcount and slug)
        finally:
            conn.close()

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

    def emailless_leads(self) -> list[dict[str, Any]]:
        """Every lead with no email yet, id ASC — the Overture backfill's
        working set (claimed or not: the phone-keyed dataset join is the
        same ground truth for a shared-pool row as for a claimed one)."""
        conn = self._conn()
        try:
            cur = conn.execute(
                """
                SELECT id, phone, person_name, business_name, trade, city,
                       state, source_url
                FROM phone_leads
                WHERE email = ''
                ORDER BY id ASC
                """
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in rows]
        finally:
            conn.close()

    def list_owned(
        self, user_id: str, trade: str = "", state: str = "", city: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Today's still-owned claims across all searches and states (UTC).

        A new UTC day starts a blank sheet; previous claims remain exclusive
        and their immutable claim/call history stays queryable by date.
        """
        frag, args = self._serve_clauses(trade, state, city)
        conn = self._conn()
        try:
            cur = conn.execute(
                f"""
                SELECT l.*, o.created_at AS claimed_at FROM phone_leads l
                JOIN phone_lead_owners o ON o.lead_id = l.id
                AND o.user_id = ?
                AND substr(o.created_at, 1, 10) = ?{frag}
                ORDER BY o.created_at DESC, l.id DESC
                LIMIT ?
                """,
                [user_id, _now()[:10], *args, limit],
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in rows]
        finally:
            conn.close()

    def claim_visibility_by_user(self) -> list[dict[str, Any]]:
        """Admin report — per user: how many claims the sheet SHOWS (their
        latest batch) and how many sit hidden behind it.

        Hidden is not lost: every hidden row is still owned, so it serves to
        nobody. The report exists because the user asked for the old stock to
        be invisible in the app but still countable by the admin (this is the
        data they will manage at main deploy).
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT o.user_id,
                       COUNT(*) AS total,
                       SUM(CASE WHEN substr(o.created_at, 1, 10) = ?
                                THEN 1 ELSE 0 END) AS visible,
                       MIN(o.created_at) AS first_claimed,
                       MAX(o.created_at) AS last_claimed
                FROM phone_lead_owners o
                GROUP BY o.user_id
                ORDER BY total DESC
                """,
                (_now()[:10],),
            ).fetchall()
            out = []
            for user_id, total, visible, first_seen, last_seen in rows:
                out.append({
                    "user_id": user_id,
                    "total": int(total),
                    "visible": int(visible or 0),
                    "hidden": int(total) - int(visible or 0),
                    "first_claimed": first_seen or "",
                    "last_claimed": last_seen or "",
                })
            return out
        finally:
            conn.close()

    def pool_stats(self) -> dict[str, Any]:
        """Honest pool inventory: totals + per-trade counts (admin/diagnostic).

        ``claimed``/``unclaimed`` are both COUNTED over the same row set, never
        derived by subtraction: a historical retire that left an ownership row
        behind (29 of them were found live on 2026-09-16) made the subtracted
        number disagree with what would actually serve — and the Location
        counts are only useful if they cannot drift.
        """
        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM phone_leads").fetchone()[0]
            claimed = conn.execute(
                "SELECT COUNT(*) FROM phone_leads l WHERE EXISTS ("
                "  SELECT 1 FROM phone_lead_owners o WHERE o.lead_id = l.id)"
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

    @staticmethod
    def _insert_call_event(
        conn: sqlite3.Connection, lead: dict[str, Any], user_id: str,
        action: str,
    ) -> dict[str, Any]:
        ts = _now()
        cur = conn.execute(
            "INSERT INTO phone_call_events (user_id, lead_id, phone, "
            "person_name, business_name, trade, state, action, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, lead["id"], lead["phone"], lead.get("person_name", ""),
             lead.get("business_name", ""), lead.get("trade", ""),
             lead.get("state", ""), action, ts),
        )
        return {"id": cur.lastrowid, "action": action, "created_at": ts}

    def record_call_event(
        self, lead_id: int, user_id: str, action: str,
    ) -> dict[str, Any] | None:
        """Owner-only dial/copy or non-terminal call outcome."""
        if action not in ("dialed", "copied", "not_interested", "follow_up", "no_answer"):
            raise ValueError("unknown phone call action")
        lead = self._owned_lead(lead_id, user_id)
        if lead is None:
            return None
        conn = self._conn()
        try:
            result = self._insert_call_event(conn, lead, user_id, action)
            conn.commit()
            return result
        finally:
            conn.close()

    def call_activity(self, user_id: str, day: str = "") -> dict[str, Any]:
        """One UTC day's immutable call history and last outcome per phone."""
        day = day or _now()[:10]
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM phone_call_events WHERE user_id = ? "
                "AND substr(created_at, 1, 10) = ? ORDER BY id ASC",
                (user_id, day),
            )
            cols = [d[0] for d in cur.description]
            events = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        finally:
            conn.close()
        outcomes: dict[str, str] = {}
        for event in events:
            if event["action"] not in ("dialed", "copied"):
                outcomes[event["phone"]] = event["action"]
        counts: dict[str, int] = {}
        for action in outcomes.values():
            counts[action] = counts.get(action, 0) + 1
        return {
            "date": day,
            "dialed": len({e["phone"] for e in events if e["action"] == "dialed"}),
            "outcomes": counts,
            "events": list(reversed(events)),
        }

    def call_days(self, user_id: str) -> list[str]:
        conn = self._conn()
        try:
            return [row[0] for row in conn.execute(
                "SELECT DISTINCT substr(created_at, 1, 10) AS day "
                "FROM phone_call_events WHERE user_id = ? "
                "ORDER BY day DESC", (user_id,),
            ).fetchall()]
        finally:
            conn.close()

    def mark_wrong_number(
        self, lead_id: int, user_id: str,
    ) -> dict[str, Any] | None:
        """Suppress a bad number, remove it from the sheet, retain archive."""
        lead = self._owned_lead(lead_id, user_id)
        if lead is None:
            return None
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_call_event(conn, lead, user_id, "wrong_number")
            cur = conn.execute(
                "INSERT INTO phone_wrong_archive "
                "(user_id, phone, snapshot_json, created_at) VALUES (?, ?, ?, ?)",
                (user_id, lead["phone"], json.dumps(lead), _now()),
            )
            conn.execute(
                "DELETE FROM phone_lead_owners WHERE lead_id IN "
                "(SELECT id FROM phone_leads WHERE phone = ?)",
                (lead["phone"],),
            )
            conn.execute("DELETE FROM phone_leads WHERE phone = ?", (lead["phone"],))
            conn.execute(
                "INSERT OR IGNORE INTO phone_suppressions (phone, reason, created_at) "
                "VALUES (?, 'wrong_number', ?)", (lead["phone"], _now()),
            )
            conn.commit()
            return {"archive_id": cur.lastrowid, "retired": True}
        finally:
            conn.close()

    def list_wrong_archive(self) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT id, user_id, phone, created_at FROM phone_wrong_archive "
                "WHERE recovered_at = '' ORDER BY id DESC"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        finally:
            conn.close()

    def recover_wrong_number(self, archive_id: int) -> bool:
        """Admin-only caller: restore exact archived row to the shared pool."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT phone, snapshot_json FROM phone_wrong_archive "
                "WHERE id = ? AND recovered_at = ''", (archive_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            suppression = conn.execute(
                "SELECT reason FROM phone_suppressions WHERE phone = ?", (row[0],),
            ).fetchone()
            if suppression is None or suppression[0] != "wrong_number":
                conn.rollback()
                return False
            lead = json.loads(row[1])
            fields = (
                "phone", "person_name", "business_name", "trade", "city", "state",
                "source", "license_status", "source_url", "email", "email_source",
                "website", "enriched_at", "created_at", "updated_at",
                "trade_evidence_url", "trade_evidence_kind", "trade_evidence_ref",
                "trade_checked_at", "voicemail_count", "voicemail_at",
            )
            names = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            inserted = conn.execute(
                f"INSERT OR IGNORE INTO phone_leads ({names}) VALUES ({marks})",
                [lead.get(name, "") for name in fields],
            )
            if not inserted.rowcount:
                conn.rollback()
                return False
            conn.execute("DELETE FROM phone_suppressions WHERE phone = ?", (row[0],))
            conn.execute(
                "UPDATE phone_wrong_archive SET recovered_at = ? WHERE id = ?",
                (_now(), archive_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()

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
        conn = self._conn()
        try:
            self._insert_call_event(conn, lead, user_id, "lead")
            conn.commit()
        finally:
            conn.close()
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
            conn = self._conn()
            try:
                self._insert_call_event(conn, lead, user_id, "voicemail")
                conn.commit()
            finally:
                conn.close()
            self._retire(lead_id, lead["phone"], "voicemail_retired")
            return {
                "retired": True, "voicemail_count": count, "cooldown_days": 0,
            }
        cooldown = VOICEMAIL_COOLDOWN_DAYS.get(count, 60)
        conn = self._conn()
        try:
            self._insert_call_event(conn, lead, user_id, "voicemail")
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
