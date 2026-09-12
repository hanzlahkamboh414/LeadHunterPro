"""Deterministic query-yield learning loop (CLAUDE.md §3/§7/§8/§11).

The old hardcoded query lists were right ONCE: numbers like "the BBB dork was
cited by 5 of 506 dossiers, so it is dropped" were a hand measurement baked
into the source. This module makes that judgment continuous and live, without
any extra AI call or provider credit.

Each search TEMPLATE (a stable label, not the concrete per-domain query) records
whether the URLs it returned ever became CITED evidence — and, of those, as
VERIFIED facts. After ``MIN_TRIALS`` real dispatched runs with zero verified
citations, the template is auto-dropped so no future lead spends a provider
credit on it. Until a template has enough data it is KEPT (default-keep is the
safe choice: dropping on one bad run would starve the pipeline).

SEGMENT-LEVEL KEYS (Phase F — the user's "yield ko GLOBAL se SEGMENT-LEVEL pe
le jao"): a deep template's yield is no longer averaged across every company
type. "deep:license" can be great for a General Contractor and zero for a
marine/heavy-civil firm; a single averaged number hides both. So each deep
template also records a row per CONFIRMED segment (``deep:license`` +
``segment=GC``), while screening templates stay GLOBAL (their job is identity
confirmation, not niche yield — the segment is a different model for them).

The segment decision uses a strict hierarchy (Problem 2, mandatory):
  * a GLOBAL drop is authoritative — never overridden, one-way;
  * a segment decides for itself only once it has its OWN ``MIN_TRIALS``;
  * otherwise it falls back to the global decision (KEEP default).

The segment bucket is computed by :func:`confirmed_bucket` from company text
and the profile's relevance term lists — NEVER from the AI's industry label,
so a marine firm the AI mislabels "GC" still buckets marine_heavycivil (kills
the mislabel contamination path, Problem 4/P-C).

Only new dossiers feed the loop (existing dossiers cannot be attributed to a
template retrospectively), so the first runs simply accumulate — pruning starts
no earlier than it has evidence for.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)

#: Dispatched runs before a zero-verified template is considered proven useless.
MIN_TRIALS = 12

#: Proportional prune (2026-09-12). The 0-verified rule never fires once a
#: template has a stray verified citation, so a chronically weak dork kept its
#: full dispatch budget forever — measured live on production: deep:bidaward
#: sat at 155 trials / 35 verified (23%) and deep:hiring at 151 / 39 (26%)
#: while screening:email ran at 93%. Once a template has PROP_MIN_TRIALS of
#: evidence, a persistently poor verified-rate is proven waste too. 30% keeps
#: deep:expansion (37%) and deep:license (54%) alive — only the true laggards
#: go.
PROP_MIN_TRIALS = 40
PROP_MAX_VERIFIED_RATE = 0.30

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "..", "output", "lead_research.db")

#: Serializes writes across concurrent research threads (LEADS_CONCURRENCY).
_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Confirmed segment buckets (Phase F) — deterministic, NEVER from the AI label
# ---------------------------------------------------------------------------
#
# P-C ("bucket contamination usi mislabel bug se"): the industry_bucket must
# NOT come from the AI-written industry string. A marine firm the AI mislabels
# "GC" would otherwise poison the GC segment's yield. So the bucket is derived
# by :func:`confirmed_bucket` from company/domain/fact TEXT against the same
# relevance term lists the re-gate uses — one boundary, one source of truth.

#: The three v1 buckets (P-C: keep it a 3-way split, no over-ambition).
BUCKET_ON_VERTICAL = "on_vertical"
BUCKET_MARINE_HEAVY = "marine_heavycivil"
BUCKET_OTHER = "other"

#: Building-trades terms that put a company in the on_vertical bucket. Mirrors
#: ``_TARGET_TRADE_DEFAULTS`` so the allow-list and the profile stay aligned.
_ON_VERTICAL_TERMS = (
    "general contractor", "construction", "subcontractor", "builder",
    "building", "commercial", "residential", "roofing", "electrical",
    "plumbing", "hvac", "mechanical", "masonry", "concrete", "framing",
    "drywall", "painting", "flooring", "glazing", "landscaping",
    "site work", "demolition", "steel", "structural",
)

#: Marine / heavy-civil terms — real construction but a DIFFERENT procurement
#: rhythm from a building GC, so it keeps its OWN yield key (never merge with
#: on_vertical, else one class's yield masks the other's). Deliberately disjoint
#: from the profile's off-vertical list (bridge/highway/road stay OFF-vertical
#: per company_profile — the exclusion check below wins for those).
_MARINE_HEAVY_CIVIL_TERMS = (
    "marine", "maritime", "dredg", "harbor", "port", "dock", "pier",
    "seawall", "heavy civil", "heavy highway", "heavy construction",
    "mass earthwork", "bulk excavation", "coastal", "shipyard",
    "lock", "dam", "levee", "channel", "breakwater", "wetland",
)


def confirmed_bucket(*, company: str = "", domain: str = "", facts: str = "") -> str:
    """Deterministic 3-way segment bucket for a company (P-C).

    Reads ONLY company/domain/fact text against the profile's relevance term
    lists — the AI's stored ``industry`` label is never consulted, so a marine
    firm the AI mislabels "GC" still buckets ``marine_heavycivil`` (kills the
    mislabel contamination path). Order of checks:

    1. an off-vertical / non-client exclusion match  -> ``other`` (not a deep
       lead at all; bucketed only so nothing leaks into a real bucket)
    2. a marine / heavy-civil term                    -> ``marine_heavycivil``
    3. a building-trades (target) term                -> ``on_vertical``
    4. otherwise                                      -> ``other``
    """
    from app.company_profile import get_profile

    prof = get_profile()
    blob = " ".join([(company or ""), (domain or ""), (facts or "")]).lower()
    if prof.is_off_vertical(blob) or any(t in blob for t in prof.non_client_terms):
        return BUCKET_OTHER
    if any(t in blob for t in _MARINE_HEAVY_CIVIL_TERMS):
        return BUCKET_MARINE_HEAVY
    if any(t in blob for t in _ON_VERTICAL_TERMS):
        return BUCKET_ON_VERTICAL
    return BUCKET_OTHER


#: The compound-key separator between a template and its segment when rows are
#: keyed in ``all()`` output / logs (e.g. ``deep:license|on_vertical``).
_SEGMENT_SEP = "|"


class QueryYieldStore:
    """Persistent record of per-template citation yield.

    One small table in the lead-research DB (``query_template_yield``). Each row
    is a template label with:
      trials   — times the template was dispatched (search actually issued)
      cited    — of those, runs where >=1 returned URL was later cited in evidence
      verified — of those, runs where a returned URL was cited as "verified"
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        # A fresh connection per call: the store is shared across concurrent
        # research threads (run_research's pool), and reusing one connection
        # across threads is not safe in SQLite. Writes are serialized under the
        # module lock; reads are cheap.
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS query_template_yield (
            template TEXT NOT NULL,
            segment TEXT NOT NULL DEFAULT '',
            trials INTEGER NOT NULL DEFAULT 0,
            cited INTEGER NOT NULL DEFAULT 0,
            verified INTEGER NOT NULL DEFAULT 0,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (template, segment)
        )
        """

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = self._conn()
        try:
            conn.execute(self._SCHEMA)
            # Phase F migration — live DBs predate the segment column and have a
            # single-column (template) PK. Additive rebuild: every legacy row
            # becomes the GLOBAL row (segment=''), and the PK widens to
            # (template, segment). No historical count is lost or misread.
            pk = [r[1] for r in conn.execute("PRAGMA table_info(query_template_yield)") if r[5] > 0]
            if pk != ["template", "segment"]:
                conn.execute("ALTER TABLE query_template_yield RENAME TO _query_template_yield_old")
                conn.execute(self._SCHEMA)
                conn.execute(
                    "INSERT INTO query_template_yield "
                    "(template, segment, trials, cited, verified, last_seen) "
                    "SELECT template, '', trials, cited, verified, last_seen "
                    "FROM _query_template_yield_old"
                )
                conn.execute("DROP TABLE _query_template_yield_old")
            conn.commit()
        finally:
            conn.close()

    def upsert(
        self, template: str, *, segment: str = "",
        trials: int = 1, cited: int = 0, verified: int = 0,
    ) -> None:
        """Add one run's outcome to the (template, segment) cumulative counts.

        ``segment=""`` is the GLOBAL row (all buckets aggregated); a non-empty
        segment is one bucket's independent row (deep templates only).
        """
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO query_template_yield (template, segment, trials, cited, verified, last_seen)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(template, segment) DO UPDATE SET
                        trials = trials + excluded.trials,
                        cited = cited + excluded.cited,
                        verified = verified + excluded.verified,
                        last_seen = CURRENT_TIMESTAMP
                    """,
                    (template, segment, trials, cited, verified),
                )
                conn.commit()
            finally:
                conn.close()

    def get(self, template: str, segment: str = "") -> dict[str, int] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT trials, cited, verified FROM query_template_yield "
                "WHERE template = ? AND segment = ?",
                (template, segment),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {"trials": row[0], "cited": row[1], "verified": row[2]}

    def all(self) -> dict[str, dict[str, int]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT template, segment, trials, cited, verified FROM query_template_yield"
            ).fetchall()
        finally:
            conn.close()
        out: dict[str, dict[str, int]] = {}
        for tpl, seg, trials, cited, verified in rows:
            key = tpl if not seg else f"{tpl}{_SEGMENT_SEP}{seg}"
            out[key] = {"trials": trials, "cited": cited, "verified": verified}
        return out

    def should_skip(self, template: str, segment: str = "") -> bool:
        """True to drop this template+segment on the next run.

        Segment hierarchy (Phase F, Problem 2 — mandatory):
          * a GLOBAL drop is authoritative — never overridden, one-way (a dead
            template never costs another 12 trials per bucket to confirm);
          * a segment decides for itself only once it has its OWN ``MIN_TRIALS``;
          * otherwise it falls back to the global decision (KEEP default).
        A deciding row drops when it has enough real trials AND never once
        produced a verified citation, OR — the proportional prune — when
        PROP_MIN_TRIALS trials show a persistently poor verified-rate (a
        stray citation no longer immunizes a 23% dork forever). No record →
        keep.
        """
        global_row = self.get(template, "")
        if global_row is not None and self._proven_dead(global_row):
            return True  # global DROP — authoritative
        if segment:
            seg_row = self.get(template, segment)
            if seg_row is not None and self._proven_dead(seg_row):
                return True
        return False  # global KEEP default

    @staticmethod
    def _proven_dead(row: dict[str, int]) -> bool:
        """A yield row is proven dead: enough trials AND (never a verified
        citation OR a persistently poor verified-rate — see PROP_MIN_TRIALS)."""
        trials = row["trials"]
        if trials < MIN_TRIALS:
            return False
        if row["verified"] == 0:
            return True
        return (trials >= PROP_MIN_TRIALS
                and row["verified"] / trials < PROP_MAX_VERIFIED_RATE)

    def delete_template(self, template: str) -> None:
        """Manual, logged override — kill a template across ALL segments.

        Resurrection is a HUMAN decision only (P-G: never auto-promote). The
        one-way global drop stays permanent until the user resets it here.
        """
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "DELETE FROM query_template_yield WHERE template = ?", (template,)
                )
                conn.commit()
            finally:
                conn.close()

    def reset(self) -> None:
        """Test helper — clear all yield rows."""
        with _write_lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM query_template_yield")
                conn.commit()
            finally:
                conn.close()


class QueryYieldPlanner:
    """One research call's accumulator for the learn loop.

    Created fresh per ``agent.research()`` call (an agent is shared across
    concurrent threads, so per-call state must never live on the agent). The
    research stages call :meth:`note` as searches are issued; the agent calls
    :meth:`commit` once, after the dossier is complete, so counts are written
    exactly once per lead with full citation context.
    """

    def __init__(self, store: QueryYieldStore | None = None) -> None:
        self._store = store
        # Keyed by (template, segment): the SEGMENT is captured at issue time
        # and never rewritten (P-D immutability — a trial is credited to the
        # label it was dispatched under, even if the commit-time picture differs).
        self._template_urls: dict[tuple[str, str], list[str]] = {}

    @property
    def enabled(self) -> bool:
        return self._store is not None

    def note(self, template: str, urls: list[str], *, segment: str = "") -> None:
        """Record the URLs a search template returned for this lead.

        ``segment`` is the CONFIRMED bucket the template was dispatched for
        (issue-time label — immutable). Screening templates pass ``""`` (global);
        deep templates pass their confirmed bucket.
        """
        if not template or not urls:
            return
        seen = self._template_urls.setdefault((template, segment), [])
        for u in urls:
            if u and u not in seen:
                seen.append(u)

    def should_skip(self, template: str, segment: str = "") -> bool:
        """Ask the persistent loop whether to drop this template+segment."""
        return bool(self._store) and self._store.should_skip(template, segment)

    def commit(self, cited_urls: set[str], verified_urls: set[str]) -> None:
        """Persist every observed template's yield against the dossier evidence.

        ``cited_urls`` — all source_urls actually cited by the dossier.
        ``verified_urls`` — the subset cited with confidence ``verified``.
        A template is credited when one of its returned URLs appears in the
        cited (or verified) set.

        Two rows per deep template are written: the GLOBAL row (all buckets
        aggregated — the authoritative DROP signal) and the SEGMENT row (that
        bucket's independent evidence). Screening templates write only the
        global row.
        """
        if self._store is None or not self._template_urls:
            return
        # Global row written ONCE per template (a template dispatched under one
        # bucket still credits the global row exactly once per run).
        by_template: dict[str, list[str]] = {}
        for (tpl, _seg), urls in self._template_urls.items():
            by_template.setdefault(tpl, []).extend(urls)
        for template, urls in by_template.items():
            hit_cited = any(u in cited_urls for u in urls)
            hit_verified = any(u in verified_urls for u in urls)
            self._store.upsert(
                template, segment="",
                trials=1,
                cited=1 if hit_cited else 0,
                verified=1 if hit_verified else 0,
            )
        # Segment rows for the deep (bucketed) dispatches.
        for (template, segment), urls in self._template_urls.items():
            if not segment:
                continue
            hit_cited = any(u in cited_urls for u in urls)
            hit_verified = any(u in verified_urls for u in urls)
            self._store.upsert(
                template, segment=segment,
                trials=1,
                cited=1 if hit_cited else 0,
                verified=1 if hit_verified else 0,
            )
        if logger.isEnabledFor(logging.INFO):
            total = len(by_template)
            verified = sum(
                1 for u in by_template.values() if any(x in verified_urls for x in u)
            )
            logger.info(
                "Query-yield recorded %d template(s), %d with a verified citation",
                total, verified,
            )