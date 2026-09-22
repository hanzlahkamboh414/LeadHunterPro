"""Scout proposals — the AI add-side, GROUNDED in catalog reality.

Mirrors the Phase H add-side (:mod:`app.discovery.template_generation`):
an LLM occasionally proposes NEW sources, proposals land in the
quarantine store (:class:`~app.source_scout.store.ScoutStore`), and every
one of them earns its place through mechanical verify → agnes probation —
never by being proposed (§12: a proposal is not evidence).

The scout is grounded, not a guesser (the 2026-09-14 test-server run
proved a flash LLM cannot name Socrata datasets: one hallucinated a 404
dataset id, one wrote a non-SODA URL). Generation is therefore TWO
narrow AI calls wrapped around real fetches:

  1. CATALOG  :mod:`~app.source_scout.catalog` fetches REAL datasets
     (live ids, domains, column names) for the uncovered states.
  2. SELECT   the AI picks up to N candidates and maps their REAL
     columns (phone / person / trade / status). The endpoint is built
     from the catalog row — the AI never writes a URL or an id.
  3. SAMPLE   one real fetch per selected dataset pulls the DISTINCT
     values of its trade column.
  4. MAP      the AI maps only those REAL values onto the canonical 14
     trade slugs — it never invents a value.

HARD GUARDS (same discipline as Phase H):
  * FLOOD — generation holds while the quarantine is already full of
    unverified proposals; the verifier/probation must consume the
    backlog first (``MAX_PENDING``).
  * SANITIZE — every selection must name a catalog candidate's real id
    and real columns; every mapped trade value must be one of the
    sampled values; the source_id must be a fresh lowercase slug. A
    missing/failed catalog is an honest zero — the AI is NEVER asked to
    invent datasets as a fallback.
  * HONEST — the entry always returns an explainable dict
    (proposed / rejected+reasons / reason / catalog summary). A guard
    failure, an LLM error, or an empty reply is a LOUD zero, never a
    silent skip.

The LLM transport is injected (``ai_ask: Callable[[str], str]``), the
catalog fetch and value sampler too — tests never touch the network.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from app.discovery.tradefold import CANONICAL_TRADES
from app.harvester.store import US_STATE_ABBRS

logger = logging.getLogger(__name__)

#: Generation holds while this many unverified proposals sit in the
#: quarantine — the staged-trust pipeline (verify → probation) must
#: consume the backlog before the AI is asked for more. Bounded for the
#: same reason Phase H bounds its request count: flooding the verifier
#: with proposals before any of them accumulates evidence buys nothing.
MAX_PENDING = 12

#: How many datasets one generation may select from the real catalog
#: candidates (was _REQUEST_COUNT — the ask is now "select from this
#: list", so the bound guards the SAMPLE fetches that follow).
MAX_SELECTIONS = 4

#: source_id shape — lowercase slug. Anything else is an AI spelling
#: invention that would fragment the dedupe keys.
_RE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_]{1,60}$")

#: ``…/resource/<dataset-id>.json`` — pulls the 4x4 out of an endpoint
#: so a re-proposed dataset is caught whatever slug the AI gives it.
_RE_DATASET = re.compile(r"/resource/([a-z0-9]{4,10}(?:-[a-z0-9]{4})?)\.json",
                         re.IGNORECASE)

#: The 50 states + DC, for gap computation. Imported shape mirrors the
#: harvester's map (kept local — a scout bug must not take the
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

#: Selection keys the SELECT prompt requests and sanitize requires —
#: ``status_column`` is optional (empty string when the dataset has no
#: license-status column).
_SELECTION_KEYS = (
    "source_id", "dataset_id", "trade_column", "phone_column",
    "person_column",
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


def _extract_array(reply: str) -> list[dict[str, Any]] | None:
    """Pull JSON objects out of an LLM reply, or None when nothing
    parses.

    Tries the whole reply as JSON first, then the substring between the
    first ``[`` and the last ``]`` (survives prose and code fences).
    ``[]`` is a VALID empty array (the AI selecting nothing); None is a
    blank/unparseable reply — the retry logic must tell them apart.
    """
    if not reply:
        return None
    text = reply.strip()
    try:
        data = json.loads(text)
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
    return None


def _parse_proposals(reply: str) -> list[dict[str, Any]]:
    """Back-compat wrapper: [] for anything unparseable (an unusable
    reply is the caller's honest zero, never a crash)."""
    parsed = _extract_array(reply)
    return parsed if parsed is not None else []


def _ask_parsed(ai_ask: Callable[[str], str], prompt: str,
                stage: str) -> tuple[list[dict[str, Any]], bool]:
    """One AI call plus ONE retry on a blank/unparseable reply.

    Returns ``(parsed, malformed)``. A VALID empty array (``[]`` — the
    model selected or mapped nothing) is ``([], False)`` and is not
    retried. Two blank/unparseable replies are ``([], True)`` so the
    caller can say ``malformed_response_retries_exhausted`` instead of
    treating a transport glitch as an empty selection. Transport
    exceptions propagate — the caller reports them as an LLM failure,
    not a crash.
    """
    for attempt in (1, 2):
        reply = ai_ask(prompt)
        parsed = _extract_array(reply or "")
        if parsed is not None:
            return parsed, False
        logger.info(
            "scout-gen: %s stage — blank/unparseable reply "
            "(attempt %d, %d chars)", stage, attempt, len(reply or ""),
        )
    return [], True


def _used_dataset_ids(store: Any) -> set[str]:
    """Dataset 4x4s already serving or already proposed — a catalog
    candidate with one is a duplicate whatever slug the AI gives it."""
    used: set[str] = set()

    def grab(endpoint: str) -> None:
        m = _RE_DATASET.search(endpoint or "")
        if m:
            used.add(m.group(1).lower())

    from app.phones import soda as _soda
    for url in (_soda.WA_DATASET, _soda.TDLR_DATASET):
        grab(url)
    for row in store.list_quarantine(limit=1000):
        grab(row.get("endpoint", ""))
    for status in ("promoted", "retired"):
        for row in store.list_status(status, limit=1000):
            grab(row.get("endpoint", ""))
    return used


def _sanitize_selection(
    raw: dict[str, Any],
    candidate: dict[str, Any],
    *,
    known: dict[str, dict],
    existing_ids: set[str],
) -> tuple[dict[str, Any] | None, str]:
    """Validate one SELECT reply object against its REAL catalog row.

    Shape rules (§12 — garbage never enters the quarantine):
      * all required keys present and non-empty where strings;
      * source_id is a lowercase slug, not a known route (good or dead)
        and not an existing quarantined/promoted/retired source;
      * every named column is a real column of the chosen dataset —
        the AI must map reality, never invent column names.
    """
    for key in _SELECTION_KEYS:
        val = raw.get(key)
        if not (isinstance(val, str) and val.strip()):
            return None, f"missing/empty {key!r}"

    source_id = raw["source_id"].strip().lower()
    if not _RE_SLUG.match(source_id):
        return None, f"source_id {source_id!r} is not a lowercase slug"
    if source_id in known:
        return None, (
            f"source_id {source_id!r} is already a KNOWN route "
            f"({known[source_id]['status']}: {known[source_id]['reason']})"
        )
    if source_id in existing_ids:
        return None, f"source_id {source_id!r} already exists in the store"

    if candidate is None:
        return None, (
            "dataset_id is not one of the real catalog candidates — "
            "never invent a dataset id"
        )
    for col_key in ("trade_column", "phone_column", "person_column"):
        col = raw[col_key].strip()
        if col not in candidate["columns"]:
            return None, (
                f"{col_key} {col!r} is not a real column of dataset "
                f"{candidate['id']} ({candidate['domain']}) — never "
                "invent column names"
            )
    status_col = str(raw.get("status_column", "")).strip()
    if status_col and status_col not in candidate["columns"]:
        return None, (
            f"status_column {status_col!r} is not a real column of "
            f"dataset {candidate['id']} — never invent column names"
        )

    return {
        "source_id": source_id,
        "candidate": candidate,
        "trade_column": raw["trade_column"].strip(),
        "phone_column": raw["phone_column"].strip(),
        "person_column": raw["person_column"].strip(),
        "status_column": status_col,
    }, ""


def _sanitize_trade_values(
    raw: dict[str, Any],
    *,
    sampled: list[str],
    source_id: str,
) -> tuple[dict[str, list[str]] | None, str]:
    """Validate one MAP reply object: trade slugs ⊆ the canonical 14,
    mapped values ⊆ the REAL sampled values of the dataset."""
    if raw.get("source_id", "").strip().lower() != source_id:
        return None, "source_id does not match the selection"
    trade_values = raw.get("trade_values")
    if not isinstance(trade_values, dict) or not trade_values:
        return None, "trade_values must be a non-empty object"
    bad = [t for t in trade_values if t not in CANONICAL_TRADES]
    if bad:
        return None, f"unknown trade slug(s) {bad} — canonical 14 only"
    allowed = set(sampled)
    for slug, values in trade_values.items():
        if not isinstance(values, list) or not values or not all(
                isinstance(v, str) and v.strip() for v in values):
            return None, f"trade_values[{slug!r}] must be non-empty strings"
        invented = [v for v in values if v not in allowed]
        if invented:
            return None, (
                f"trade_values[{slug!r}] invents value(s) "
                f"{invented[:3]!r} — only the sampled real values may "
                "be mapped"
            )
    return {
        t: [str(v).strip() for v in vs]
        for t, vs in trade_values.items()
    }, ""


def _selection_prompt(candidates: list[dict[str, Any]], covered: dict[str,
                                                                     list[str]],
                      known: dict[str, dict], trades: str) -> str:
    covered_lines = "\n".join(
        f"  {state}: covered by {', '.join(ids)}"
        for state, ids in sorted(covered.items())
    ) or "  (none — every state is a gap)"
    dead_lines = "\n".join(
        f"  - {sid} ({info['status']}): {info['reason']}"
        for sid, info in sorted(known.items())
    ) or "  (none recorded)"
    cand_lines = []
    for c in candidates:
        cols = ", ".join(
            f"{f} ({label})" if label and label.lower() != f else f
            for f, label in c["column_labels"].items()
        )[:1200]
        cand_lines.append(
            f"- id={c['id']} domain={c['domain']} state={c['state']} "
            f"name={c['name']!r}\n  columns: {cols}\n"
            f"  description: {c['description'][:250]}"
        )
    return (
        "You scout OPEN-DATA sources of US contractor license records "
        "for a lead platform. A usable source is a Socrata/SODA dataset "
        "whose rows are licensed contractors, with a PHONE number "
        "column, a PERSON name column (owner/principal — a person, not "
        "a company), a trade/license-type column we can map, and "
        "ideally a status/expiry column to filter ACTIVE licenses.\n"
        "FACTS you must respect:\n"
        "- Government license data NEVER contains email.\n"
        "- The datasets below were fetched LIVE from the Socrata "
        "catalog: real IDs, real domains, real column names. NEVER "
        "invent a dataset id, endpoint, or column name — anything not "
        "listed is rejected.\n"
        "- These routes are ALREADY KNOWN (do not re-select them or "
        "the same dataset under a new name):\n"
        f"{covered_lines}\n"
        "- These routes are PROVEN DEAD (never propose them):\n"
        f"{dead_lines}\n"
        f"- Only these trade slugs exist: {trades}\n"
        f"Select up to {MAX_SELECTIONS} of these real candidates "
        "(prefer state contractor-license boards):\n"
        + "\n".join(cand_lines)
        + "\nAnswer as a JSON array, one object per selection, each "
        "exactly:\n"
        '{"source_id": "lowercase_snake_slug", "dataset_id": "<id from '
        'the list>", "trade_column": "<real column>", "phone_column": '
        '"<real column>", "person_column": "<real column>", '
        '"status_column": "<real column or empty>"}\n'
        "Nothing else in your answer. If none qualify, return []."
    )


def _mapping_prompt(blocks: list[tuple[str, str, list[str]]],
                    trades: str) -> str:
    """blocks: [(source_id, trade_column, sampled_values), ...]"""
    dataset_lines = []
    for source_id, column, values in blocks:
        shown = json.dumps(values[:200])
        dataset_lines.append(
            f"- {source_id} (column {column!r}) values: {shown}"
        )
    return (
        "Map REAL license-type column values onto the canonical trade "
        "slugs of a contractor lead platform. Only these slugs exist:\n"
        f"{trades}\n"
        "Each dataset's values below were fetched LIVE from it — map "
        "ONLY values that appear in its list; NEVER invent a value. Map "
        "a value to at most one slug, and leave unrelated values "
        "(accounting, irrigation, …) unmapped:\n"
        + "\n".join(dataset_lines)
        + "\nAnswer as a JSON array, one object per dataset you could "
        "map (omit a dataset entirely when none of its values fit), "
        "each exactly:\n"
        '{"source_id": "...", "trade_values": {"gc": '
        '["General Contractor"], ...}}\n'
        "Nothing else in your answer."
    )


def generate_source_proposals(
    store: Any,
    ai_ask: Callable[[str], str] | None = None,
    *,
    existing_coverage: dict[str, dict[str, str]] | None = None,
    provenance: str = "playbook",
    catalog_fetch: Callable[[set[str]], dict[str, Any]] | None = None,
    value_sampler: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The single scout generation entry: grounded, guarded, honest.

    Args:
        store: :class:`ScoutStore` (quarantine + known/dead memory).
        ai_ask: ``(prompt) -> str`` LLM transport; defaults to the app's
            ``make_ai_ask()`` on the scout's own key lane.
        existing_coverage: hand-connector coverage
            (``soda.TRADE_COVERAGE`` shape: slug -> {state: source_id}).
            Defaults to the live map.
        provenance: provenance string stamped on proposals.
        catalog_fetch: ``(gap_states) -> catalog dict``; defaults to the
            real Socrata catalog fetch (tests inject a fake).
        value_sampler: ``(endpoint, column) -> {"values": [...]}``;
            defaults to the real SODA sample fetch.

    Returns ``{"proposed": [...], "rejected": [{"source_id", "reason"}],
    "reason": "", "catalog": {...}}`` — always explainable, never a
    silent skip. When the catalog has no real candidates the AI is NOT
    called: the guess-mode of the old design is gone.
    """
    if ai_ask is None:
        from app.ai.gateway import make_ai_ask
        from app.core.config import settings

        ai_ask = make_ai_ask(api_key=settings.AI_API_KEY_3)
    if catalog_fetch is None:
        from app.source_scout.catalog import fetch_catalog_candidates
        catalog_fetch = fetch_catalog_candidates
    if value_sampler is None:
        from app.source_scout.catalog import sample_column_values
        value_sampler = sample_column_values

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
    gap_states = _US_STATES - set(covered)
    if not gap_states:
        return {
            "proposed": [], "rejected": [],
            "reason": "no gap states — every state already has coverage",
        }

    known = store.known_map()
    catalog = catalog_fetch(gap_states)
    candidates = list(catalog.get("candidates") or [])
    used = _used_dataset_ids(store)
    fresh = [c for c in candidates if c["id"].lower() not in used]
    summary = {
        "gap_states": sorted(gap_states),
        "queries": catalog.get("queries", []),
        "errors": catalog.get("errors", []),
        "candidates": len(candidates),
        "fresh": len(fresh),
    }
    if not fresh:
        return {
            "proposed": [], "rejected": [],
            "reason": (
                f"catalog pre-fetch found no fresh real candidates "
                f"({len(candidates)} fetched, all already in use or "
                f"known) — the AI is never asked to guess"
            ),
            "catalog": summary,
        }

    existing_ids = {r["source_id"] for r in store.list_quarantine(limit=1000)}
    existing_ids |= {r["source_id"] for r in store.list_status("promoted",
                                                               limit=1000)}
    existing_ids |= {r["source_id"] for r in store.list_status("retired",
                                                               limit=1000)}
    by_dataset = {c["id"]: c for c in fresh}
    trades = ", ".join(CANONICAL_TRADES)

    # -- stage 1: SELECT from the real candidates ------------------------
    try:
        selections_raw, select_malformed = _ask_parsed(
            ai_ask, _selection_prompt(fresh, covered, known, trades),
            "select",
        )
    except Exception as exc:  # noqa: BLE001 — a dead/provided AI is honest
        logger.warning("scout-gen: LLM call failed: %s", exc)
        return {"proposed": [], "rejected": [],
                "reason": f"LLM call failed: {exc}", "catalog": summary}

    selections: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for raw in selections_raw[:MAX_SELECTIONS]:
        cand = by_dataset.get(str(raw.get("dataset_id", "")).strip())
        clean, reason = _sanitize_selection(
            raw, cand, known=known, existing_ids=existing_ids)
        if clean is None:
            sid = str(raw.get("source_id", "?"))[:80]
            rejected.append({"source_id": sid, "reason": reason})
            continue
        existing_ids.add(clean["source_id"])
        selections.append(clean)
    if not selections:
        if select_malformed:
            reason = "malformed_response_retries_exhausted"
        elif not selections_raw:
            reason = (
                "no_candidates_qualified: AI selected none of the "
                f"{len(fresh)} real candidates"
            )
        else:
            reason = (
                "every selection was rejected (invented id/column — "
                "see rejected reasons)"
            )
        return {
            "proposed": [], "rejected": rejected,
            "reason": reason,
            "catalog": summary,
        }

    # -- stage 2: SAMPLE real values, then MAP them ----------------------
    from app.source_scout.catalog import build_endpoint

    sampled: dict[str, dict[str, Any]] = {}
    for sel in selections:
        endpoint = build_endpoint(sel["candidate"])
        sampled[sel["source_id"]] = value_sampler(
            endpoint, sel["trade_column"])
        if not sampled[sel["source_id"]].get("values"):
            rejected.append({
                "source_id": sel["source_id"],
                "reason": (
                    "could not sample real trade values for column "
                    f"{sel['trade_column']!r} "
                    f"({sampled[sel['source_id']].get('error', 'empty')})"
                ),
            })
    mappable = [s for s in selections
                if sampled[s["source_id"]].get("values")]
    if not mappable:
        return {
            "proposed": [], "rejected": rejected,
            "reason": "no dataset yielded real trade values to map",
            "catalog": summary,
        }

    try:
        mappings_raw, map_malformed = _ask_parsed(
            ai_ask,
            _mapping_prompt(
                [(s["source_id"], s["trade_column"],
                  sampled[s["source_id"]]["values"]) for s in mappable],
                trades,
            ),
            "map",
        )
    except Exception as exc:  # noqa: BLE001 — a dead/provided AI is honest
        logger.warning("scout-gen: LLM call failed: %s", exc)
        return {"proposed": [], "rejected": rejected,
                "reason": f"LLM call failed: {exc}", "catalog": summary}

    if map_malformed:
        return {
            "proposed": [], "rejected": rejected,
            "reason": "malformed_response_retries_exhausted",
            "catalog": summary,
        }

    proposed: list[str] = []
    by_sid = {str(m.get("source_id", "")).strip().lower(): m
              for m in mappings_raw}
    for sel in mappable:
        sid = sel["source_id"]
        raw = by_sid.get(sid)
        if raw is None:
            rejected.append({
                "source_id": sid,
                "reason": "no trade mapping returned (AI omitted it — "
                          "its values fit no canonical slug)",
            })
            continue
        trade_values, reason = _sanitize_trade_values(
            raw, sampled=sampled[sid]["values"], source_id=sid)
        if trade_values is None:
            rejected.append({"source_id": sid, "reason": reason})
            continue
        cand = sel["candidate"]
        if store.propose(
                sid, "soda", cand["name"], build_endpoint(cand),
                {
                    "state": cand["state"],
                    "trade_column": sel["trade_column"],
                    "trade_values": trade_values,
                    "phone_column": sel["phone_column"],
                    "person_column": sel["person_column"],
                    "status_column": sel["status_column"],
                }, provenance):
            proposed.append(sid)
        else:
            rejected.append({
                "source_id": sid,
                "reason": "already exists in the store",
            })

    if not proposed:
        return {
            "proposed": [], "rejected": rejected,
            "reason": "no proposal survived selection + real-value "
                      "mapping (see rejected reasons)",
            "catalog": summary,
        }
    return {"proposed": proposed, "rejected": rejected, "reason": "",
            "catalog": summary}
