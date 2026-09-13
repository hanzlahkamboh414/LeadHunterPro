"""Phones search service — pool-first serve, live SODA gap-fill.

The P7 architecture shape, in place from day one: a phone search serves
what the shared pool already holds (pure SQL, instant), then live-fetches
ONLY the gap from the license-board sources, stocking EVERYTHING fetched
(other-trade rows are pool inventory — the P2 banking rule) and serving
the matching remainder. Honest telemetry at every step; no source = an
honest "no coverage" reason, never fake results (CLAUDE.md §1).
"""

from __future__ import annotations

import logging
from typing import Any

from app.discovery.tradefold import normalize_trade
from app.phones.soda import covered_sources, fetch_license_records
from app.phones.store import PhoneLeadsStore

logger = logging.getLogger(__name__)


def phone_search(
    store: PhoneLeadsStore,
    *,
    trade: str,
    state: str,
    city: str,
    target: int,
    user_id: str,
) -> dict[str, Any]:
    """Serve ``target`` phone leads for one user; gap-fill live when short.

    Returns an honest outcome dict::

        {
            "leads": [...],          # served (exclusively owned) leads
            "served_from_pool": n,   # served instantly from the pool
            "fetched_live": n,       # rows the sources returned
            "stocked_new": n,        # rows newly added to the pool
            "banked_other_trade": n, # fetched rows of ANOTHER trade (inventory)
            "dropped_bad_phone": n,  # fetched rows without a usable phone
            "coverage": [...],       # source ids that (could) serve this trade
            "reason": "",            # honest shortfall/coverage reason
        }
    """
    slug = normalize_trade(trade)
    # Fail-open (the P2 rule): a search trade that folds to '' has no gate —
    # but the phone sources are keyed BY slug, so an unfoldable trade also
    # has no source to fetch from. The pool can still serve whatever it has.
    target = max(1, min(target, 5000))
    state = (state or "").strip().upper()[:2]
    city = (city or "").strip()

    leads = store.serve(slug, state, city, target, user_id)
    served_from_pool = len(leads)

    outcome: dict[str, Any] = {
        "leads": leads,
        "served_from_pool": served_from_pool,
        "fetched_live": 0,
        "stocked_new": 0,
        "banked_other_trade": 0,
        "dropped_bad_phone": 0,
        "coverage": covered_sources(slug, state),
        "reason": "",
    }

    if len(leads) >= target:
        return outcome

    # Gap-fill: fetch the shortfall (+ a small buffer so the next search of
    # the same trade hits the pool, not the network) from each covering
    # source until the target is met.
    need = target - len(leads)
    for source_id in outcome["coverage"]:
        if len(leads) >= target:
            break
        status, records, meta = fetch_license_records(
            source_id, slug, city=city, limit=need + 50,
        )
        if status.value != "success":
            logger.info(
                "phone gap-fill: source %s returned %s (%s)",
                source_id, status.value, meta.get("error", ""),
            )
            continue
        outcome["fetched_live"] += len(records)
        counts = store.add(records)
        outcome["stocked_new"] += counts["inserted"]
        outcome["dropped_bad_phone"] += counts["dropped_bad_phone"]
        outcome["banked_other_trade"] += (
            len(records) - counts["inserted"] - counts["duplicate"]
            - counts["dropped_bad_phone"]
        )
        # exclude this run's earlier serves so the gap-fill re-serve only
        # brings NEW rows (never a duplicate of the pool serve).
        leads += store.serve(
            slug, state, city, target - len(leads), user_id,
            exclude_ids=[l["id"] for l in leads],
        )

    outcome["leads"] = leads
    if len(leads) < target:
        if not outcome["coverage"]:
            outcome["reason"] = (
                f"no phone source covers trade '{trade}'"
                + (f" in {state}" if state else " in any state")
                + " yet — pool served what it had"
            )
        else:
            outcome["reason"] = (
                f"sources returned fewer usable leads than the target "
                f"({len(leads)}/{target} served)"
            )
    return outcome
