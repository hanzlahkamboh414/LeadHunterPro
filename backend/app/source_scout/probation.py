"""Agnes probation — the second trust stage + auto promote/retire
(P9 sub-inc 4).

The mechanical verifier (:mod:`app.source_scout.verifier`) proved the
source serves REAL numbers once. Probation asks whether it keeps doing
so — and whether a human would call the data usable:

  * MECHANICAL GATE — every probation check re-fetches a filtered
    sample first. A source that cannot serve rows fails the check
    BEFORE agnes is ever asked (the P6 demand-gated lesson: the AI
    never spends without a logged fetch).
  * AGNES JUDGEMENT — agnes sees a small sample of REAL rows and
    judges data quality: a person column full of company names,
    redacted/placeholder values, or obvious test data is exactly the
    kind of junk the column-existence checks cannot see. Her verdict is
    recorded as a ``probation``-stage verdict — the only promotion
    currency (verified-only reward; an AI label never promotes anything
    by itself, it merely contributes evidence).
  * AUTO-PROMOTE/RETIRE — after ``PROBATION_MIN_TRIALS`` checks the
    score decides, with NO admin anywhere (founder directive): pass
    rate >= ``PROMOTE_THRESHOLD`` promotes, anything below retires with
    the honest score in the reason. Fewer trials than that = not enough
    evidence, the source simply stays on probation.
  * CIRCUIT BREAKER — a PROMOTED source that starts failing in
    production is auto-retired after ``CONSECUTIVE_PROD_FAILS_TO_RETIRE``
    consecutive failures (the search-registry breaker's semantics —
    fail enough, open the circuit — adapted to the lifecycle: retirement
    with a human-only resurrection, the Phase H contract).

Honesty rules: an LLM error or an unparseable reply records NO verdict —
our AI being down must not fail a source that is serving fine; the
check reports the reason and the source stays wherever it was.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from app.discovery.sources._http import FetchResult
from app.discovery.sources.status import SourceStatus
from app.source_scout.store import (
    STAGE_PROBATION,
    STATUS_PROBATION,
    STATUS_PROMOTED,
    STATUS_VERIFIED,
    ScoutStore,
)
from app.source_scout.verifier import SAMPLE_LIMIT, _fetch_rows

logger = logging.getLogger(__name__)

#: Probation checks required before the score is allowed to decide
#: anything. Fewer than this is "not enough evidence" — the source
#: stays on probation whatever its early pass rate.
PROBATION_MIN_TRIALS = 5

#: The promote threshold (founder directive: agnes probation >= 70%).
#: At or above it the source is promoted; below it (after enough
#: trials) it is retired.
PROMOTE_THRESHOLD = 0.7

#: Consecutive production failures that auto-retire a promoted source
#: (the circuit breaker). A single passing outcome resets the streak.
CONSECUTIVE_PROD_FAILS_TO_RETIRE = 3

#: Verdict stage for post-promotion health outcomes recorded by the
#: harvest lane (a promoted source still gets watched).
STAGE_PRODUCTION = "production"

#: Rows shown to agnes per judgement — enough to see person/phone/trade
#: quality, small enough to keep the prompt a rounding error.
MAX_ROWS_TO_SHOW_AI = 3


def default_ai_ask() -> Callable[[str], str]:
    """The scout's deep lane (make_ai_ask defaults to AI_MODEL_DEEP).

    Public: adapter writing and probation judgement are the same kind of
    step on the same key — one seam for the whole scout, never a second.
    """
    from app.ai.gateway import make_ai_ask
    from app.core.config import settings

    return make_ai_ask(api_key=settings.AI_API_KEY_3)


def parse_agnes_reply(reply: str) -> tuple[bool | None, str]:
    """(verdict, reason) from an agnes judgement reply.

    Public: every scout lane that asks agnes for a PASS/FAIL judgement
    (V1 probation, V2 probation) reads the reply the same way.

    Accepts the requested "PASS/FAIL + one sentence" shape and tolerates
    case/prefix noise. A reply with neither keyword (or both, equally
    ambiguous) is an honest ``(None, "unparseable")`` — no verdict.
    """
    text = (reply or "").upper()
    p, f = text.find("PASS"), text.find("FAIL")
    if p == -1 and f == -1:
        return None, "unparseable reply"
    if f == -1 or (p != -1 and p < f):
        return True, (reply or "").strip()[:200]
    return False, (reply or "").strip()[:200]


def run_probation_check(
    store: ScoutStore,
    source_id: str,
    *,
    fetch_fn: Callable[..., FetchResult] | None = None,
    pinned_get: Callable[..., FetchResult] | None = None,
    ai_ask: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """One probation check: mechanical gate, then agnes, then advance.

    Returns ``{"source_id", "passed", "recorded", "agnes_reason",
    "score", "status"}`` — ``recorded`` says whether a verdict row was
    written (an LLM error/unparseable reply records none).
    """
    from app.discovery.sources._http import fetch as _default_fetch
    from app.source_scout.verifier import _pinned_get as _default_pinned

    fetch_fn = fetch_fn or _default_fetch
    pinned_get = pinned_get or _default_pinned

    row = store.get(source_id)
    if not row:
        raise ValueError(f"unknown source_id: {source_id!r}")
    if row["status"] not in (STATUS_VERIFIED, STATUS_PROBATION):
        return {
            "source_id": source_id, "passed": False, "recorded": False,
            "agnes_reason": (
                f"status is {row['status']!r} — nothing to probation"
            ),
            "score": None, "status": row["status"],
        }
    if row["status"] == STATUS_VERIFIED:
        store.start_probation(source_id)

    payload = row["payload"]
    first_slug = next(iter(payload["trade_values"]))
    values = ", ".join(
        f"'{v.replace(chr(39), chr(39) * 2)}'"
        for v in payload["trade_values"][first_slug]
    )
    status, rows, reason = _fetch_rows(
        fetch_fn, pinned_get, row["endpoint"],
        {"$where": f"{payload['trade_column']} IN({values})",
         "$limit": str(SAMPLE_LIMIT)},
        pinned_ip=str(payload.get("pinned_ip", "")),
    )
    usable = [
        r for r in (rows or [])
        if isinstance(r, dict)
        and str(r.get(payload["phone_column"], "") or "").strip()
    ]
    if status is not SourceStatus.SUCCESS or not usable:
        store.record_verdict(
            source_id, STAGE_PROBATION, False,
            f"mechanical gate: no usable rows ({reason})")
        decision = advance_probation(store, source_id)
        return {
            "source_id": source_id, "passed": False, "recorded": True,
            "agnes_reason": "", "score": store.probation_score(source_id),
            "status": decision,
        }

    # Mechanical gate passed — agnes may now judge real rows.
    if ai_ask is None:
        ai_ask = default_ai_ask()
    sample = []
    for r in usable[:MAX_ROWS_TO_SHOW_AI]:
        cols = [payload["phone_column"], payload["person_column"],
                payload["trade_column"]]
        if payload.get("status_column"):
            cols.append(payload["status_column"])
        if "business_name" in r:
            cols.append("business_name")
        sample.append({
            c: str(r.get(c, ""))[:60] for c in cols if c in r
        })
    prompt = (
        "You judge whether rows from a US contractor-license data source "
        "are REAL, USABLE lead data. A usable row names a PERSON (the "
        "license holder / owner — not a company), carries a phone number, "
        "and a trade classification. Company names in the person column, "
        "redacted or placeholder values, or obvious test data make a "
        "source unusable.\n"
        f"Sample rows:\n{json.dumps(sample, indent=1)}\n"
        "Answer exactly PASS or FAIL, then one short sentence why."
    )
    try:
        reply = ai_ask(prompt)
    except Exception as exc:  # noqa: BLE001 — our AI being down is not the source's fault
        logger.warning("scout-probation %s: LLM call failed: %s",
                       source_id, exc)
        return {
            "source_id": source_id, "passed": False, "recorded": False,
            "agnes_reason": f"LLM call failed: {exc}",
            "score": store.probation_score(source_id),
            "status": store.get(source_id)["status"],
        }

    verdict, why = parse_agnes_reply(reply)
    if verdict is None:
        logger.info("scout-probation %s: unparseable reply: %.200s",
                    source_id, reply)
        return {
            "source_id": source_id, "passed": False, "recorded": False,
            "agnes_reason": "unparseable agnes reply",
            "score": store.probation_score(source_id),
            "status": store.get(source_id)["status"],
        }

    store.record_verdict(
        source_id, STAGE_PROBATION, verdict,
        f"agnes: {why}" if verdict else f"agnes FAIL: {why}")
    decision = advance_probation(store, source_id)
    return {
        "source_id": source_id, "passed": verdict, "recorded": True,
        "agnes_reason": why, "score": store.probation_score(source_id),
        "status": decision,
    }


def advance_probation(store: ScoutStore, source_id: str) -> str:
    """Promote / retire / keep on probation from the current score.

    Called after every check. With fewer than ``PROBATION_MIN_TRIALS``
    verdicts the answer is always "probation" — evidence first.
    """
    row = store.get(source_id)
    if not row:
        raise ValueError(f"unknown source_id: {source_id!r}")
    if row["status"] != STATUS_PROBATION:
        return row["status"]

    passes, total, rate = store.probation_score(source_id)
    if total < PROBATION_MIN_TRIALS:
        return STATUS_PROBATION
    if rate >= PROMOTE_THRESHOLD:
        store.promote(source_id)
        logger.info("scout-probation %s: PROMOTED (%d/%d = %.0f%%)",
                    source_id, passes, total, rate * 100)
        return STATUS_PROMOTED
    store.retire(
        source_id,
        f"probation score {passes}/{total} = {rate:.0%} "
        f"< {PROMOTE_THRESHOLD:.0%} threshold",
    )
    logger.info("scout-probation %s: RETIRED (%d/%d = %.0f%%)",
                source_id, passes, total, rate * 100)
    return "retired"


def probation_pass(
    store: ScoutStore,
    *,
    fetch_fn: Callable[..., FetchResult] | None = None,
    pinned_get: Callable[..., FetchResult] | None = None,
    ai_ask: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """One pass over the trust pipeline's probation population.

    Every ``verified`` source enters probation and gets one check; every
    ``probation`` source gets one check. Each check immediately advances
    its source (promote/retire at threshold). The worker calls this on a
    schedule — spacing passes over time is what makes a pass RATE mean
    something.

    Returns an honest summary: ``{"started", "checked", "promoted",
    "retired", "skipped": {source_id: reason}}``.
    """
    started: list[str] = []
    checked: list[str] = []
    promoted: list[str] = []
    retired: list[str] = []
    skipped: dict[str, str] = {}

    targets = [r["source_id"] for r in store.list_status(STATUS_VERIFIED)]
    targets += [r["source_id"] for r in store.list_status(STATUS_PROBATION)]

    for source_id in targets:
        before = (store.get(source_id) or {}).get("status", "")
        out = run_probation_check(
            store, source_id, fetch_fn=fetch_fn, pinned_get=pinned_get,
            ai_ask=ai_ask)
        if before == STATUS_VERIFIED and out["status"] != STATUS_VERIFIED:
            started.append(source_id)
        if out["recorded"]:
            checked.append(source_id)
        else:
            skipped[source_id] = out["agnes_reason"]
        if out["status"] == STATUS_PROMOTED:
            promoted.append(source_id)
        elif out["status"] == "retired":
            retired.append(source_id)

    return {
        "started": started, "checked": checked, "promoted": promoted,
        "retired": retired, "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Production health — the circuit breaker for PROMOTED sources
# ---------------------------------------------------------------------------

def record_production_outcome(
    store: ScoutStore, source_id: str, ok: bool, detail: str = "",
) -> bool:
    """Record a promoted source's harvest outcome and auto-retire on a
    failure streak (circuit-breaker semantics: enough consecutive fails
    opens the circuit; one pass closes it).

    Returns True when the streak retired the source.
    """
    store.record_verdict(
        source_id, STAGE_PRODUCTION, ok,
        ("production ok" if ok else f"production FAIL: {detail}"))
    return retire_if_broken(store, source_id)


def retire_if_broken(store: ScoutStore, source_id: str) -> bool:
    """Retire a promoted source after enough consecutive production
    failures. A passing outcome anywhere in the streak breaks it."""
    row = store.get(source_id)
    if not row or row["status"] != STATUS_PROMOTED:
        return False
    streak = 0
    for ok in store.recent_verdicts(source_id, STAGE_PRODUCTION):
        if ok:
            break
        streak += 1
    if streak >= CONSECUTIVE_PROD_FAILS_TO_RETIRE:
        store.retire(
            source_id,
            f"circuit breaker: {streak} consecutive production failures",
        )
        logger.warning(
            "scout circuit breaker: %s RETIRED after %d consecutive "
            "production failures", source_id, streak)
        return True
    return False
