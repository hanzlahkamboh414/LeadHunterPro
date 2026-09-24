"""Phones search service — pure-SQL instant serve (P7).

The P6/P7 split: the HARVESTER stocks the shared pool around the clock
(demand-ranked trade×state rotation, daily quotas, admission control), and
a phone search SERVES what the pool holds — pure SQL, instant, nothing
else. A short pool is an honest partial with a "harvester is stocking this
pair" reason, never a synchronous license-board fetch inside the request
(the old in-request gap-fill made searches slow and burned source quota at
the user's expense; stocking is the harvester's job now). No source covers
the trade = an honest "no coverage" reason, never fake results (CLAUDE.md
§1). The search itself records demand (P6 hook), so the very shortfall the
user saw is the signal that stocks the pair for their next search.

P7.5: phone users search TRADE-LESS — state + quantity only (calling is
about volume, not trades). The serve is the pool's fail-open slug='' path
(mixed trades, each row carries its own trade); coverage for a trade-less
search means "some source stocks this state", and the demand hook records
a trade='' STATE-level row so the harvester boosts every covered pair of
that state.
"""

from __future__ import annotations

import logging
from typing import Any

from app.discovery.tradefold import normalize_trade
from app.phones.soda import covered_sources, state_sources
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
    enforce_quota: bool = True,
) -> dict[str, Any]:
    """Serve ``target`` phone leads for one user from the shared pool.

    Pure SQL (P7): the serve IS the exclusivity mark — rows this user is
    handed are claimed atomically by ``store.serve`` and never served to
    anyone else. Returns an honest outcome dict::

        {
            "leads": [...],          # served (exclusively owned) leads
            "served_from_pool": n,   # served instantly from the pool
            "fetched_live": 0,       # kept for schema compat — always 0 now
            "stocked_new": 0,        # (the harvester, not the search, stocks)
            "banked_other_trade": 0,
            "dropped_bad_phone": 0,
            "coverage": [...],       # sources that (could) cover this search
            "reason": "",            # honest shortfall/coverage reason
        }
    """
    slug = normalize_trade(trade)
    # Fail-open (the P2 rule): a search trade that folds to '' has no gate —
    # the pool serves whatever it holds, mixed trades, each row labelled.
    target = max(1, min(target, 5000))
    state = (state or "").strip().upper()[:2]
    city = (city or "").strip()

    leads = store.serve(slug, state, city, target, user_id,
                        enforce_quota=enforce_quota)
    if slug:
        coverage = covered_sources(slug, state)
    else:
        # Trade-less search (P7.5): coverage = anything stocking this state.
        coverage = state_sources(state)
    outcome: dict[str, Any] = {
        "leads": leads,
        "served_from_pool": len(leads),
        # Stocking telemetry keys stay in the response (schema compat): the
        # SEARCH never stocks any more — these are the harvester's counters.
        "fetched_live": 0,
        "stocked_new": 0,
        "banked_other_trade": 0,
        "dropped_bad_phone": 0,
        "coverage": coverage,
        "reason": "",
    }
    logger.info(
        "phone search (pure SQL): trade=%r state=%r city=%r served %d/%d "
        "from pool for %s",
        slug, state, city, len(leads), target, user_id,
    )

    if len(leads) >= target:
        return outcome

    if not slug and store.unqualified_count(state, city):
        outcome["reason"] = (
            f"pool served {len(leads)}/{target} — raw business records are "
            "pending trade verification and cannot serve yet"
        )
    elif not coverage:
        outcome["reason"] = (
            f"no phone source covers trade '{trade}'"
            if slug else f"no phone source covers {state or 'any state'} yet"
        ) + " — pool served what it had"
    else:
        # The demand hook (P6) already recorded this search, so the
        # harvester's next pass stocks exactly this gap.
        what = "trade+state pair" if slug else "state"
        outcome["reason"] = (
            f"pool served {len(leads)}/{target} — the background harvester "
            f"is stocking this {what}; try again in a few minutes"
        )
    return outcome
