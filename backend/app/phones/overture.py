"""Overture Maps — the phones vertical's email ground truth (P8).

Overture's places theme (74M businesses, CDLA Permissive 2.0, free) is the
only open dataset that carries EMAILS alongside phone numbers. License
boards never publish emails (verified 2026-09-13) — Overture closes that
gap with one phone-keyed join.

Shape (verified live 2026-09-14 against release 2026-08-19.0):

* ``s3://overturemaps-us-west-2/release/<date>/theme=places/type=place/*``
  — ~20 zstd Parquet files, anonymous S3 reads, us-west-2.
* ``phones`` / ``emails`` / ``websites`` are plain VARCHAR lists; the
  numbers are E.164 (``+15125551234``), the same form
  :func:`app.phones.store.normalize_phone` emits.

A live ``list_has_any(phones, [...])`` scan over S3 takes ~7 MINUTES (it
cannot push a nested-list predicate into Parquet metadata, so every row is
read) — per-lead lookups at that price are not a stage, they are a denial
of service. So this module MATERIALIZES the join once:

    sync()          one ~10-minute bulk pass: S3 -> slim local DuckDB
                    table (phone, email, website) for every place that
                    has an email. Monthly re-run picks up the new release.
    lookup_emails() batch lookup against the LOCAL table — milliseconds,
                    and what the enrich worker's stage 0 actually calls.

The local file lives in ``output/`` (gitignored, like every runtime DB).
Until the first sync runs, ``lookup_emails`` returns {} honestly and the
enrich worker simply falls through to its normal web stages — stage 0 is
an accelerator, never a dependency (CLAUDE.md §4: one source disappearing
must not break the pipeline).

Everything duckdb-touching is injected or deferred so tests stay hermetic
and the worker's import of this module never costs a duckdb import.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(
    os.path.dirname(__file__), "..", "..", "output", "overture.duckdb"
)

#: Anonymous-S3 read of the places theme (CDLA Permissive 2.0, no key).
_S3_GLOB_TEMPLATE = (
    "s3://overturemaps-us-west-2/release/{release}"
    "/theme=places/type=place/*"
)

_sync_lock = threading.Lock()


def _s3_con():
    """A throwaway DuckDB connection configured for anonymous S3 reads."""
    import duckdb  # deferred: only sync/_latest_release need it

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2'")
    con.execute("SET enable_progress_bar=false")
    return con


def _latest_release(con: Any) -> str:
    """Newest dated release directory (e.g. ``2026-08-19.0``), '' if none.

    Release names are ISO dates (optionally ``.N``-suffixed), so a
    lexicographic max IS the newest one.
    """
    rows = con.execute(
        "SELECT max(regexp_extract(file, 'release/([^/]+)/theme=places', 1)) "
        "FROM glob("
        "'s3://overturemaps-us-west-2/release/*/theme=places/type=place/*')"
    ).fetchall()
    return (rows[0][0] or "") if rows else ""


class OvertureStore:
    """The materialized phone -> email join, in its own ``overture.duckdb``.

    One DB per concern (campaigns.db pattern): Overture data is neither
    pool inventory (phone_leads.db) nor research state (lead_research.db)
    — it is a reference dataset, a phone-keyed dictionary.
    """

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = _DEFAULT_DB
        self._db_path = os.fspath(db_path)

    # -- one-time (monthly) materialization ------------------------------------

    def sync(
        self,
        *,
        s3_con: Any = None,
        release: str = "",
        source_glob: str = "",
    ) -> dict[str, Any]:
        """(Re)build the local table from an Overture release.

        ``s3_con`` / ``source_glob`` are the hermetic-test seams: a fake
        connection over a local Parquet file exercises the REAL sync SQL
        without network. Returns counts for the log/report.

        ``arg_min(website, email)`` keeps the website from the SAME row
        the picked email came from — pairing by provenance, never a mix.
        """
        with _sync_lock:
            source = source_glob or _S3_GLOB_TEMPLATE.format(release=release)
            if not source_glob:
                con = s3_con or _s3_con()
                release = release or _latest_release(con)
                if not release:
                    raise RuntimeError(
                        "no Overture places release found under "
                        "s3://overturemaps-us-west-2/release/"
                    )
                source = _S3_GLOB_TEMPLATE.format(release=release)

            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            import duckdb  # deferred (see module docstring)

            local = duckdb.connect(self._db_path)
            try:
                local.execute("BEGIN TRANSACTION")
                local.execute(f"""
                    CREATE OR REPLACE TABLE place_contacts AS
                    SELECT phone,
                           min(email) AS email,
                           arg_min(website, email) AS website
                    FROM (
                        SELECT
                            regexp_replace(p, '[^0-9+]', '', 'g') AS phone,
                            email, website
                        FROM (
                            SELECT unnest(phones) AS p,
                                   lower(emails[1]) AS email,
                                   coalesce(websites[1], '') AS website
                            FROM read_parquet('{source}')
                            WHERE emails IS NOT NULL
                              AND len(emails) > 0
                              AND contains(emails[1], '@')
                        )
                    )
                    WHERE phone <> ''
                    GROUP BY phone
                """)
                local.execute(
                    "CREATE OR REPLACE TABLE meta AS "
                    "SELECT ? AS release, strftime(now(), '%Y-%m-%dT%H:%M:%SZ') AS synced_at",
                    [release or "(test source)"],
                )
                # The ART index turns the worker's batch IN-lookups from a
                # 27.9M-row scan (~5s) into an instant seek — measured live.
                local.execute(
                    "CREATE INDEX IF NOT EXISTS idx_place_contacts_phone "
                    "ON place_contacts(phone)"
                )
                local.execute("COMMIT")
                contacts = local.execute(
                    "SELECT count(*) FROM place_contacts"
                ).fetchone()[0]
            finally:
                local.close()
            logger.info(
                "overture sync: %d phone->email contacts from %s",
                contacts, release or source,
            )
            return {"contacts": contacts, "release": release or "(test source)"}

    # -- lookups (the enrich worker's stage 0) ---------------------------------

    def is_synced(self) -> bool:
        """True once the local table exists — the cheap gate stage 0 checks."""
        return os.path.exists(self._db_path)

    def release(self) -> str:
        """Which Overture release the local table was built from, '' if none."""
        if not self.is_synced():
            return ""
        try:
            import duckdb

            con = duckdb.connect(self._db_path, read_only=True)
            try:
                return con.execute("SELECT release FROM meta").fetchone()[0]
            finally:
                con.close()
        except Exception:  # noqa: BLE001 — a stale/corrupt file is an honest ''
            logger.warning("overture meta unreadable — treating as unsynced",
                           exc_info=True)
            return ""

    def lookup_emails(self, phones: list[str]) -> dict[str, dict[str, str]]:
        """Batch phone-keyed lookup: E.164 phone -> ``{email, website}``.

        Only phones PRESENT with an email come back; a miss is absence,
        never a guess. Before the first sync (or during a re-sync, when
        DuckDB's single-writer lock refuses readers) this returns {} —
        the caller falls through to its normal stages.
        """
        wanted = [p for p in dict.fromkeys(
            (p or "").strip() for p in phones
        ) if p]
        if not wanted or not self.is_synced():
            return {}
        try:
            import duckdb
            from app.email.email_cleaner import clean_emails

            con = duckdb.connect(self._db_path, read_only=True)
            try:
                rows = con.execute(
                    "SELECT phone, email, website FROM place_contacts "
                    "WHERE phone IN (" + ",".join("?" * len(wanted)) + ")",
                    wanted,
                ).fetchall()
            finally:
                con.close()
        except Exception:  # noqa: BLE001 — stage 0 must never break enrichment
            logger.info("overture lookup unavailable — honest empty",
                        exc_info=True)
            return {}
        # The same page-furniture rules the crawl applies (§14 reuse) — now
        # the SHARED gate in ``app.email.email_cleaner`` rather than a private
        # name reached across packages, which is what let the two rule lists
        # drift apart in the first place.
        return {
            phone: {"email": email, "website": website or ""}
            for phone, email, website in rows
            if clean_emails([email])
        }
