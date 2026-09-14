"""Scout proposals — the AI add-side: the PLAYBOOK PROMPT (P9 sub-inc 2).

Mirrors the Phase H add-side (:mod:`app.discovery.template_generation`):
an LLM occasionally proposes NEW sources, proposals land in the
quarantine store (:class:`~app.source_scout.store.ScoutStore`), and every
one of them earns its place through mechanical verify → agnes probation —
never by being proposed (§12: a proposal is not evidence).

The playbook is what makes this a SCOUT and not a random idea generator:

  * WHAT a good source looks like — SODA (Socrata) open-data license
    datasets with a phone column, a person-name column, a trade/license
    column, and an active-status signal. Government license data NEVER
    carries email (verified on every source) — the playbook says so, so
    the AI does not waste proposals on email columns that do not exist.
  * WHERE to look — only UNCOVERED states. Existing connector coverage
    (``soda.TRADE_COVERAGE``) plus already-promoted scout sources are
    computed per call and the prompt asks for the gaps, never for more
    of what we have.
  * WHAT NOT to look at — the known/dead memory
    (:meth:`~app.source_scout.store.ScoutStore.known_map`), seeded from
    the paid-for lessons: public SearXNG 429s, DDG lite, CSLB's F5 WAF,
    dead Overpass. The AI never re-proposes a proven-dead route.

HARD GUARDS (same discipline as Phase H):
  * FLOOD — generation holds while the quarantine is already full of
    unverified proposals; the verifier/probation must consume the
    backlog first (``MAX_PENDING``).
  * SANITIZE — every proposal must be a real SODA shape (https resource
    URL, valid state, trades from the canonical 14, phone + person
    columns named) and must not duplicate a known/known-dead/existing
    source. Anything else is rejected with a logged reason.
  * HONEST — the entry always returns an explainable dict
    (proposed / rejected+reasons / reason). A guard failure, an LLM
    error, or an empty reply is a LOUD zero, never a silent skip.

The LLM transport is injected (``ai_ask: Callable[[str], str]``),
defaulting to the app's ``make_ai_ask()`` — tests never touch the
network.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable
from urllib.parse import urlparse

from app.discovery.tradefold import CANONICAL_TRADES
from app.harvester.store import US_STATE_ABBRS

logger = logging.getLogger(__name__)

#: Generation holds while this many unverified proposals sit in the
#: quarantine — the staged-trust pipeline (verify → probation) must
#: consume the backlog before the AI is asked for more. Bounded for the
#: same reason Phase H bounds its request count: flooding the verifier
#: with proposals before any of them accumulates evidence buys nothing.
MAX_PENDING = 12

#: How many source proposals one generation call asks for. A state
#: license board is a big commitment (a whole connector's worth of
#: coverage) — two to four per call keeps the pipeline digestible.
_REQUEST_COUNT = (2, 4)

#: source_id shape — lowercase slug. Anything else is an AI spelling
#: invention that would fragment the dedupe keys.
_RE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_]{1,60}$")

#: The 50 states + DC, for proposal validation. Imported shape mirrors
#: the harvester's map (kept local — a scout bug must not take the
#: harvester's vocabulary down with it).
_US_STATES = frozenset(US_STATE_ABBRS.values())

#: The known/dead memory seed — every route we have already paid for in
#: probe time or credits. ``seed_known()`` upserts these so the playbook
#: prompt carries them and the AI never re-proposes a dead route.
#: Reasons are the honest evidence trail (verified dates / failure mode).
KNOWN_GOOD: dict[str, str] = {
    "wa_license": (
        "data.wa.gov m8qx-ubtq — live connector (soda.py); person name "
        "+ trade tag + phone, verified 2026-09-13"
    ),
    "tdlr_license": (
        "data.texas.gov 7358-krk7 — live connector (soda.py); trade "
        "license_type + phones, verified 2026-09-13"
    ),
    "overture": (
        "Overture Maps places theme — phone→email dictionary, live "
        "(P8, bbb3cb5), 27.9M contacts"
    ),
    "nyc_portal": (
        "NYC — verified free & trade-tagged in the 2026-09-13 sweep; "
        "hand connector still pending"
    ),
    "la_portal": (
        "Los Angeles — verified free & trade-tagged in the 2026-09-13 "
        "sweep; hand connector still pending"
    ),
    "chicago_portal": (
        "Chicago — verified free & trade-tagged in the 2026-09-13 "
        "sweep; hand connector still pending"
    ),
}

KNOWN_DEAD: dict[str, str] = {
    "public_searxng": (
        "public SearXNG JSON APIs all return 429/HTML — dead, never "
        "retry (only the self-hosted EC2 instance works)"
    ),
    "ddg_lite": (
        "DuckDuckGo lite fails on quoted/site: queries — dead for our "
        "shapes, never retry"
    ),
    "cslb_portal": (
        "cslb.ca.gov ListByClassification — F5 edge rejects every scripted "
        "POST (transport fingerprint, verified 2026-09-14: even a replayed "
        "real-browser request fails); stocked browser-only via scripts/"
        "sync_cslb.py, never a scriptable SODA source — do not propose "
        "ASP.NET WebForms portals behind WAFs"
    ),
    "osm_overpass": (
        "OpenStreetMap Overpass — no usable business phone/email coverage "
        "for this vertical; dead 2026-09-13"
    ),
}

#: Proposal keys the prompt requests and sanitize requires. ``trade_column``
#: is what the verifier checks the trade_values mapping against — the
#: column NAME is dataset-specific (WA: specialtycode1desc, TDLR:
#: license_type), so the proposal must name it.
_REQUIRED_KEYS = (
    "source_id", "name", "endpoint", "state", "trade_column",
    "trade_values", "phone_column", "person_column",
)


def seed_known(store: Any) -> None:
    """Idempotently upsert the known/dead memory into the store.

    Called by the maintenance script / worker startup, not by
    ``generate_source_proposals`` — generation stays pure (reads the
    memory, never writes it).
    """
    for source_id, reason in KNOWN_GOOD.items():
        store.upsert_known(source_id, "good", reason)
    for source_id, reason in KNOWN_DEAD.items():
        store.upsert_known(source_id, "dead", reason)


def _covered_states(existing_coverage: dict[str, dict[str, str]],
                    store: Any) -> dict[str, list[str]]:
    """{state: [source_ids]} — hand connectors + promoted scout sources.

    A promoted scout source's payload carries its ``state``; that is live
    coverage the prompt must respect exactly like a hand connector's.
    """
    covered: dict[str, list[str]] = {}
    for _slug, by_state in (existing_coverage or {}).items():
        for state, source_id in (by_state or {}).items():
            covered.setdefault(state.upper(), []).append(source_id)
    for row in store.list_status("promoted"):
        full = store.get(row["source_id"])
        state = (full or {}).get("payload", {}).get("state", "")
        if state:
            covered.setdefault(state.upper(), []).append(row["source_id"])
    return covered


def _parse_proposals(reply: str) -> list[dict[str, Any]]:
    """Pull JSON proposal objects out of an LLM reply.

    Tries the whole reply as JSON first, then the substring between the
    first ``[`` and the last ``]`` — robust to an LLM that wraps the
    array in prose or a ```json fence. Malformed items are dropped here
    only if they are not dicts; shape rules live in sanitize.
    """
    if not reply:
        return []
    text = reply.strip()
    # Strip a markdown code fence if present.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    for candidate in (text,):
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except (ValueError, TypeError):
            pass
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except (ValueError, TypeError):
            pass
    return []


def _sanitize_proposal(raw: dict[str, Any], *, known: dict[str, dict],
                       existing_ids: set[str]) -> tuple[dict[str, Any] | None,
                                                        str]:
    """Validate one LLM proposal; (None, reason) when it must not enter.

    Shape rules (§12 — garbage never enters the quarantine):
      * all required keys present and non-empty where strings;
      * source_id is a lowercase slug, not a known route (good or dead)
        and not an existing quarantined/promoted/retired source;
      * endpoint is an https SODA resource URL (…/resource/….json);
      * state is a real 2-letter US state;
      * trade_values keys ⊆ the canonical 14 (an unknown trade would
        never be harvested — it would be dead weight in coverage);
      * phone_column and person_column named (the phones vertical's
        whole point).
    """
    if not isinstance(raw, dict):
        return None, "not a JSON object"
    for key in _REQUIRED_KEYS:
        val = raw.get(key)
        if isinstance(val, str):
            if not val.strip():
                return None, f"missing/empty {key!r}"
        elif not val:
            return None, f"missing/empty {key!r}"

    source_id = str(raw["source_id"]).strip().lower()
    if not _RE_SLUG.match(source_id):
        return None, f"source_id {source_id!r} is not a lowercase slug"
    if source_id in known:
        return None, (
            f"source_id {source_id!r} is already a KNOWN route "
            f"({known[source_id]['status']}: {known[source_id]['reason']})"
        )
    if source_id in existing_ids:
        return None, f"source_id {source_id!r} already exists in the store"

    endpoint = str(raw["endpoint"]).strip()
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or "/resource/" not in parsed.path \
            or not parsed.path.endswith(".json"):
        return None, (
            f"endpoint {endpoint!r} is not an https SODA resource URL "
            "(…/resource/<id>.json)"
        )

    state = str(raw["state"]).strip().upper()
    if state not in _US_STATES:
        return None, f"state {state!r} is not a US state code"

    trade_values = raw.get("trade_values")
    if not isinstance(trade_values, dict) or not trade_values:
        return None, "trade_values must be a non-empty object"
    bad = [t for t in trade_values if t not in CANONICAL_TRADES]
    if bad:
        return None, f"unknown trade slug(s) {bad} — canonical 14 only"
    for slug, values in trade_values.items():
        if not isinstance(values, list) or not values or not all(
                isinstance(v, str) and v.strip() for v in values):
            return None, f"trade_values[{slug!r}] must be non-empty strings"
    trade_column = str(raw["trade_column"]).strip()
    if not trade_column:
        return None, "missing/empty 'trade_column'"

    proposal = {
        "source_id": source_id,
        "kind": "soda",
        "name": str(raw["name"]).strip()[:200],
        "endpoint": endpoint[:500],
        "payload": {
            "state": state,
            "trade_column": trade_column,
            "trade_values": {
                t: [str(v).strip() for v in vs]
                for t, vs in trade_values.items()
            },
            "phone_column": str(raw["phone_column"]).strip(),
            "person_column": str(raw["person_column"]).strip(),
            "status_column": str(raw.get("status_column", "")).strip(),
        },
    }
    return proposal, ""


def generate_source_proposals(
    store: Any,
    ai_ask: Callable[[str], str] | None = None,
    *,
    existing_coverage: dict[str, dict[str, str]] | None = None,
    provenance: str = "playbook",
) -> dict[str, Any]:
    """The single scout generation entry: guarded, gap-targeted, honest.

    Args:
        store: :class:`ScoutStore` (quarantine + known/dead memory).
        ai_ask: ``(prompt) -> str`` LLM transport; defaults to the app's
            ``make_ai_ask()`` on the scout's own key lane.
        existing_coverage: hand-connector coverage
            (``soda.TRADE_COVERAGE`` shape: slug -> {state: source_id}).
            Defaults to the live map.
        provenance: provenance string stamped on proposals.

    Returns ``{"proposed": [...], "rejected": [{"source_id", "reason"}],
    "reason": ""}`` — always explainable, never a silent skip.
    """
    if ai_ask is None:
        from app.ai.gateway import make_ai_ask
        from app.core.config import settings

        ai_ask = make_ai_ask(api_key=settings.AI_API_KEY_3)

    pending = store.list_quarantine(limit=MAX_PENDING + 1)
    if len(pending) >= MAX_PENDING:
        return {
            "proposed": [], "rejected": [],
            "reason": (
                f"flood guard: {len(pending)} proposals already await "
                f"verify/probation (max {MAX_PENDING}) — consume first"
            ),
        }

    if existing_coverage is None:
        from app.phones import soda as _soda

        existing_coverage = _soda.TRADE_COVERAGE

    covered = _covered_states(existing_coverage, store)
    known = store.known_map()
    existing_ids = {r["source_id"] for r in store.list_quarantine(limit=1000)}
    existing_ids |= {r["source_id"] for r in store.list_status("promoted",
                                                               limit=1000)}
    existing_ids |= {r["source_id"] for r in store.list_status("retired",
                                                               limit=1000)}

    covered_lines = "\n".join(
        f"  {state}: covered by {', '.join(ids)}"
        for state, ids in sorted(covered.items())
    ) or "  (none — every state is a gap)"
    dead_lines = "\n".join(
        f"  - {sid} ({info['status']}): {info['reason']}"
        for sid, info in sorted(known.items())
    ) or "  (none recorded)"
    trades = ", ".join(CANONICAL_TRADES)
    lo, hi = _REQUEST_COUNT

    prompt = (
        "You scout OPEN-DATA sources of US contractor license records for a "
        "lead platform. A usable source is a Socrata/SODA dataset (an "
        "https URL ending /resource/<dataset-id>.json on a government open-"
        "data portal like data.oregon.gov) whose rows are licensed "
        "contractors, with these columns: a PHONE number column, a PERSON "
        "name column (owner/principal — a person, not a company), a trade/"
        "license-type column whose values we can map, and ideally a status/"
        "expiry column to filter ACTIVE licenses.\n"
        "FACTS you must respect:\n"
        "- Government license data NEVER contains email — do not look for "
        "or mention email columns.\n"
        "- These routes are ALREADY KNOWN (do not re-propose them or "
        "anything hosted the same way with the same data):\n"
        f"{covered_lines}\n"
        "- These routes are PROVEN DEAD or already covered (never propose "
        f"them):\n{dead_lines}\n"
        "- Only these 14 trade slugs exist (map dataset values onto them, "
        f"propose nothing for unmappable trades): {trades}\n"
        f"Propose {lo} to {hi} NEW sources, ONLY for US states NOT in the "
        "covered list. Prefer state contractor-license boards (electrical, "
        "plumbing, HVAC, GC) on Socrata portals.\n"
        "Answer as a JSON array, one object per source, each exactly:\n"
        '{"source_id": "lowercase_snake_slug", "name": "Human Name", '
        '"endpoint": "https://data.<state>.gov/resource/<id>.json", '
        '"state": "XX", "trade_column": "license_type_column_name", '
        '"trade_values": {"trade_slug": ["Exact Dataset Value", ...]}, '
        '"phone_column": "column_name", "person_column": '
        '"column_name", "status_column": "column_name or empty"}\n'
        "Nothing else in your answer. If you know of no real dataset for a "
        "gap state, propose fewer — never invent dataset IDs."
    )
    try:
        reply = ai_ask(prompt)
    except Exception as exc:  # noqa: BLE001 — a dead/provided AI is honest, not fatal
        logger.warning("scout-gen: LLM call failed: %s", exc)
        return {"proposed": [], "rejected": [],
                "reason": f"LLM call failed: {exc}"}

    proposed: list[str] = []
    rejected: list[dict[str, str]] = []
    for raw in _parse_proposals(reply or ""):
        clean, reason = _sanitize_proposal(
            raw, known=known, existing_ids=existing_ids)
        if clean is None:
            sid = str(raw.get("source_id", "?"))[:80]
            logger.info("scout-gen: rejected %s — %s", sid, reason)
            rejected.append({"source_id": sid, "reason": reason})
            continue
        if store.propose(
                clean["source_id"], clean["kind"], clean["name"],
                clean["endpoint"], clean["payload"], provenance):
            existing_ids.add(clean["source_id"])
            proposed.append(clean["source_id"])
        else:
            rejected.append({
                "source_id": clean["source_id"],
                "reason": "already exists in the store",
            })
    if not proposed:
        return {
            "proposed": [], "rejected": rejected,
            "reason": (
                "returned no usable new source (blank reply or nothing "
                "passed sanitize/known/dead/does-not-duplicate)"
            ),
        }
    return {"proposed": proposed, "rejected": rejected, "reason": ""}
