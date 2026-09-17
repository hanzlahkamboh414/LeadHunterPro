"""Seed lists — the deterministic input of the coverage engine (V2).

What this is NOT: AI discovery. The seed CSVs are committed, versioned
data (docs/architecture/coverage_engine_v2.md §2) — enumerable lists the
AI is explicitly forbidden from generating (founder rule, memory
ai-not-for-enumerable-lists). What this IS: the loader + invariant guard
that turns those two CSVs into registry rows, validating every row
against the same shared vocabulary the demand system uses
(``app.discovery.tradefold.CANONICAL_TRADES``) before anything leaves
the shop.

Two lists, one vocabulary:

* ``state_license_boards.csv`` — phones/address/license domain. 51 rows
  (50 states + DC), column ``trade_scope`` is the TX lesson encoded: not
  every state licenses every trade (TX has no state-level GC license), so
  the router must never bind a trade the seed says a state does not
  license. ``priority_rank`` + ``estab`` come from
  ``cbp_construction_2023.csv`` (Census CBP NAICS 23, ESTAB descending,
  PAYANN tie-break — one-time offline compute, see
  scripts/build_state_priority.py).
* ``email_sources.csv`` — email domain, same rigor: every row is a NAMED
  source with a URL, never a category placeholder. Boards mostly do NOT
  publish emails — that is why emails get their own seed tier.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.discovery.tradefold import CANONICAL_TRADES
from app.source_scout.store import (
    PROBER_PATHS,
    ScoutStore,
    get_store,
)

#: Seed dir is this module's own ``seeds/`` folder.
SEED_DIR = Path(__file__).resolve().parent / "seeds"

STATE_BOARDS_CSV = SEED_DIR / "state_license_boards.csv"
EMAIL_SOURCES_CSV = SEED_DIR / "email_sources.csv"
CBP_CSV = SEED_DIR / "cbp_construction_2023.csv"

#: trade_scope tokens outside the canonical 14 — the two honest specials.
SCOPE_ALL = "all_trades"
SCOPE_NONE = "none"
SCOPE_SPECIALS = frozenset({SCOPE_ALL, SCOPE_NONE})

#: known_access hint vocabulary — the prober's 5 paths (store.PROBER_PATHS,
#: one vocabulary) plus the proven legacy soda/socrata routes.
ACCESS_PATH_HINTS = frozenset((*PROBER_PATHS, "soda", "socrata", ""))


class SeedValidationError(ValueError):
    """The seed CSV breaks an invariant — nothing is loaded on failure."""


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def load_state_boards(path: Path | None = None) -> list[dict[str, str]]:
    """Row 1 → its own validation; raises SeedValidationError on any break.

    Invariants (docs §2.3):
    * >= 45 rows, no duplicate state codes, no duplicate priority_rank
    * trade_scope tokens ⊆ CANONICAL_TRADES ∪ {all_trades, none}
    * priority_rank/estab agree with cbp_construction_2023.csv — the
      raw CBP numbers are the re-rank source, the seed file only mirrors
      them (a drift is a broken commit, caught here)
    * known_access ⊆ the prober path vocabulary
    """
    path = path or STATE_BOARDS_CSV
    rows = _read_csv(path)
    if len(rows) < 45:
        raise SeedValidationError(
            f"{path.name}: {len(rows)} rows, need >= 45")

    states = [r["state"].strip().upper() for r in rows]
    if len(set(states)) != len(states):
        dup = {s for s in states if states.count(s) > 1}
        raise SeedValidationError(f"{path.name}: duplicate states {sorted(dup)}")

    ranks = [int(r["priority_rank"]) for r in rows]
    if len(set(ranks)) != len(ranks):
        raise SeedValidationError(f"{path.name}: duplicate priority_rank")

    cbp = {r["state"]: r for r in _read_csv(CBP_CSV)}
    for r in rows:
        state = r["state"].strip().upper()
        scope_tokens = [t.strip() for t in r["trade_scope"].split(",") if t.strip()]
        bad = [t for t in scope_tokens
               if t not in CANONICAL_TRADES and t not in SCOPE_SPECIALS]
        if bad:
            raise SeedValidationError(
                f"{path.name}: {state} trade_scope not in vocabulary: {bad}")
        if r["known_access"].strip() not in ACCESS_PATH_HINTS:
            raise SeedValidationError(
                f"{path.name}: {state} known_access "
                f"{r['known_access']!r} not a prober path")
        if state not in cbp:
            raise SeedValidationError(f"{path.name}: {state} missing from CBP")
        if int(r["priority_rank"]) != int(cbp[state]["priority_rank"]):
            raise SeedValidationError(
                f"{path.name}: {state} rank {r['priority_rank']} != CBP "
                f"{cbp[state]['priority_rank']}")
        if int(r["estab"]) != int(cbp[state]["estab"]):
            raise SeedValidationError(
                f"{path.name}: {state} estab {r['estab']} != CBP "
                f"{cbp[state]['estab']}")
    return rows


def load_email_sources(path: Path | None = None) -> list[dict[str, str]]:
    """Email seed — named rows only, same rigor as the board list.

    Invariant: >= 2 committed named sources (overture_places,
    common_crawl) each with a base_url and an estimated_rows; a
    category-only placeholder is a broken commit.
    """
    path = path or EMAIL_SOURCES_CSV
    rows = _read_csv(path)
    if len(rows) < 2:
        raise SeedValidationError(
            f"{path.name}: {len(rows)} named sources, need >= 2")
    seen: set[str] = set()
    for r in rows:
        sid = r["source_id"].strip().lower()
        if sid in seen:
            raise SeedValidationError(f"{path.name}: duplicate {sid}")
        seen.add(sid)
        if not r["base_url"].strip():
            raise SeedValidationError(f"{path.name}: {sid} has no base_url")
        if not r["estimated_rows"].strip():
            raise SeedValidationError(f"{path.name}: {sid} has no estimated_rows")
        try:
            caps = json.loads(r["capabilities_hint"])
            assert isinstance(caps, dict)
        except (ValueError, AssertionError):
            raise SeedValidationError(
                f"{path.name}: {sid} capabilities_hint not valid JSON")
        if not isinstance(caps.get("email"), dict) or \
                not caps["email"].get("present"):
            raise SeedValidationError(
                f"{path.name}: {sid} must claim email present")
        if r["seed_domain"].strip() not in ("emails", "both"):
            raise SeedValidationError(
                f"{path.name}: {sid} seed_domain not emails/both")
        if r["access_path"].strip() not in ACCESS_PATH_HINTS:
            raise SeedValidationError(
                f"{path.name}: {sid} access_path not a prober path")
    return rows


def seed_registry(store: ScoutStore | None = None) -> dict[str, int]:
    """Load both CSVs into the source registry (upsert, non-destructive).

    Returns {"boards": n, "emails": n}. Lifecycle is untouched for rows
    that already exist — re-running after statuses moved does not
    resurrect or demote anything.
    """
    store = store or get_store()
    boards = load_state_boards()
    emails = load_email_sources()
    for r in boards:
        state = r["state"].strip().upper()
        store.seed_upsert(
            f"state_{state}_board", seed_domain="phones", state=state,
            name=r["agency_name"], endpoint=r["base_url"],
            base_url=r["base_url"],
            seed_meta={
                "agency_code": r["agency_code"], "trade_scope": r["trade_scope"],
                "priority_rank": int(r["priority_rank"]),
                "estab": int(r["estab"]),
                "known_access": r["known_access"],
                "sourced_from": "state_license_boards.csv",
            },
        )
    for r in emails:
        caps = json.loads(r["capabilities_hint"])
        store.seed_upsert(
            r["source_id"].strip().lower(), seed_domain="emails",
            state="", name=r["source_id"], endpoint=r["base_url"],
            base_url=r["base_url"],
            seed_meta={
                "access_path": r["access_path"],
                "capabilities_hint": caps,
                "estimated_rows": r["estimated_rows"],
                "sourced_from": "email_sources.csv",
            },
        )
    return {"boards": len(boards), "emails": len(emails)}