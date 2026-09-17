"""Adapter Writer — the ONE AI step of the pipeline (§5, §12 Phase 3).

Everything else in Coverage Engine V2 is deterministic: seeds enumerate,
the prober walks paths, the validator measures, probation counts. This
module is where judgement is needed exactly once per source — turning a
real sample of columns + rows into a declared, reviewable contract:

    fetch_spec      how to fetch it again (re-runnable)
    file_shape      how to read those bytes (encoding/delimiter/sheet)
    field_map       canonical field → REAL source column
    capabilities    which fields exist — present-flags ONLY
    trade_mapping   source trade code → canonical slug

The reply is HARD-validated against the real sample before it is stored.
An AI that invents a column, a number or a trade slug produces a
rejected contract, not a broken source:

  * every non-null field_map value must be a column that exists in the
    sample (case-insensitive match, normalized to the real spelling)
  * capabilities carry ``present`` ONLY — no numbers, ever: fill_rate is
    the Validator's measurement (§6), not the AI's claim
  * a claimed capability must have a mapped column
  * trade slugs come from CANONICAL_TRADES, and where the source's codes
    are already known (``known_trade_mapping``, e.g. CSLB_CODE_MAP) the
    AI may not invent its own — an empty map is filled from the known one
  * ``source_id``/``state`` are the registry's values, never the reply's

Documented tightening vs the §5 example: trade_mapping VALUES are
canonical slugs (``"gc"``, ``"electrical"``) from the single taxonomy in
:mod:`app.discovery.tradefold`, not free text — one vocabulary for the
whole platform (Phase H's rule).
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlparse

from app.discovery.tradefold import CANONICAL_TRADES
from app.source_scout.store import (
    PROBER_PATHS,
    STATUS_PROBING,
    ScoutStore,
)

#: Canonical fields the adapter may map (§5).
FIELD_KEYS = ("company_name", "phone", "email", "address", "city",
              "state_field", "license_no", "trade", "status")

#: Fields a field_map MUST carry — without them nothing can be routed.
REQUIRED_FIELDS = ("company_name", "state_field")

#: Capability keys the router's quota formula knows (§8).
CAPABILITY_KEYS = ("phone", "email", "address", "city", "license_no",
                   "trade", "status")

#: fetch.method values we can actually execute.
METHODS = ("GET", "POST")

#: fetch.format values :mod:`app.source_scout.tabular` can read.
FORMATS = ("csv", "tsv", "txt", "xlsx", "excel", "zip/csv", "zip/tsv",
           "zip/txt", "zip/xlsx", "pdf", "json")

#: One retry when the reply fails hard validation (bounded — an AI that
#: fails twice has not understood the source; the registry keeps the
#: honest failure instead of storing a guessed contract).
MAX_ATTEMPTS = 2

#: Sample rows shown to the AI — enough to see real values, small enough
#: to keep the prompt a rounding error.
MAX_SAMPLE_ROWS = 3
MAX_CELL_CHARS = 60


class AdapterError(ValueError):
    """The reply is not a usable contract (or the AI could not be reached)."""


# ---------------------------------------------------------------------------
# reading a stored adapter back
# ---------------------------------------------------------------------------

def adapter_of(row: dict[str, Any]) -> dict[str, Any]:
    """One registry row → the adapter contract dicts (JSON columns parsed).

    The single reader every consumer uses — dryrun, probation, and the
    Phase-4 orchestrator — so no caller parses these columns by hand.
    """
    out: dict[str, Any] = {}
    for col in ("fetch_spec", "field_map", "trade_mapping", "capabilities"):
        raw = row.get(col)
        if isinstance(raw, dict):
            out[col] = raw
            continue
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            parsed = {}
        out[col] = parsed if isinstance(parsed, dict) else {}
    return out


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------

def build_prompt(*, source_id: str, state: str, access_path: str,
                 columns: list[str], sample_rows: list[dict[str, Any]],
                 source_url: str = "",
                 form_facts: dict[str, Any] | None = None,
                 known_trade_mapping: dict[str, str] | None = None) -> str:
    """The §5 contract request — real columns + real sample values in,
    strict JSON out. No prose is accepted back."""
    sample = [
        {k: str(v)[:MAX_CELL_CHARS] for k, v in r.items()}
        for r in sample_rows[:MAX_SAMPLE_ROWS]
    ]
    trade_note = ""
    if known_trade_mapping:
        trade_note = (
            "\nThis source's trade codes are ALREADY KNOWN. Copy this "
            "trade_mapping EXACTLY (do not add, drop or rename a code):\n"
            f"{json.dumps(known_trade_mapping, sort_keys=True)}\n")
    url_note = ""
    if source_url:
        url_note = (f"\nThe registry's URL for this source is {source_url} — "
                    f"'fetch.url' must be that URL (or a path under it); "
                    f"never invent a different host.\n")
    form_note = ""
    if form_facts:
        form_note = (
            "\nThe data comes from a FORM POST. These are the page's REAL "
            "controls (read off the live page) — your fetch must use them "
            "and nothing else:\n"
            f"  select fields and their accepted values: "
            f"{json.dumps(form_facts.get('selects', {}), sort_keys=True)}\n"
            f"  submit buttons: {json.dumps(form_facts.get('submits', []))}\n"
            "So the fetch object must also carry:\n"
            '  "form": {"select_field": "<a select name above>", '
            '"code": "<one of that select\'s values>", '
            '"submit_field": "<a submit name above>", "submit_value": "..."}\n'
            "Only the fields the page actually has may appear in the spec.\n")
    return f"""You are writing one source adapter for a company-discovery engine.
Source: {source_id} (jurisdiction {state or "n/a"}), reached via the
"{access_path}" access path.{url_note}

REAL COLUMNS in the fetched table ({len(columns)}):
{json.dumps(columns)}

REAL SAMPLE ROWS (values exactly as they appear):
{json.dumps(sample, indent=1)}
{form_note}
Return ONE JSON object, no prose, no markdown fences:
{{
 "fetch": {{"method": "GET or POST", "url": "<the exact URL that returns this data>",
            "format": "one of {list(FORMATS)}", "refresh": "monthly|weekly|annual",
            "file_shape": {{"inner_path": "", "delimiter": ",", "encoding": "utf-8",
                            "header_row": 1, "zip_bomb_max_mb": 500}}}},
 "field_map": {{"<canonical field>": "<exact column name from the list above>"}},
 "capabilities": {{"<field>": {{"present": true|false}}}},
 "trade_mapping": {{"<source code>": "<canonical trade slug>"}},
 "estimated_rows": <integer>
}}

RULES — a contract that breaks any of these is rejected:
1. field_map keys must come from {list(FIELD_KEYS)}; it MUST include
   company_name and state_field. Values must be columns that EXIST in
   the list above, spelled exactly as shown. Use null for an absent field.
2. capabilities may only use these keys: {list(CAPABILITY_KEYS)}.
   Values are {{"present": true}} or {{"present": false}} — a boolean and
   nothing else. NEVER write a number or a fill rate.
   A capability may only be present:true if field_map maps its column.
3. trade_mapping maps the source's own trade/license codes to canonical
   slugs. Allowed slugs: {list(CANONICAL_TRADES)}.
   Use {{}} when the source publishes no trade codes.
   Never invent a code or a slug.{trade_note}
4. file_shape must be explicit: for "zip/*" formats inner_path is
   required; give the real delimiter, encoding and header row for CSV.
5. estimated_rows is your rough guess at the full file size; the
   validator re-measures it, so do not inflate it.
6. "url" must be a real https:// URL that returns this exact data."""


# ---------------------------------------------------------------------------
# reply parsing + hard validation
# ---------------------------------------------------------------------------

def parse_reply(text: str) -> dict[str, Any]:
    """The reply as JSON — tolerating fences and surrounding prose."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw[3:]
        raw = raw.removeprefix("json").strip()
    try:
        out = json.loads(raw)
    except ValueError:
        start = raw.find("{")
        if start == -1:
            raise AdapterError("reply carried no JSON object") from None
        try:
            out, _ = json.JSONDecoder().raw_decode(raw[start:])
        except ValueError as exc:
            raise AdapterError(f"reply is not valid JSON: {exc}") from exc
    if not isinstance(out, dict):
        raise AdapterError(f"reply is a {type(out).__name__}, not an object")
    return out


def _match_column(value: Any, columns: list[str]) -> str:
    """The real column a field_map value names, or '' — the anti-
    hallucination gate: an invented column never reaches the store."""
    want = str(value or "").strip().lower()
    if not want:
        return ""
    for col in columns:
        if col.strip().lower() == want:
            return col
    return ""


def _norm_field_map(raw: Any, columns: list[str]) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise AdapterError("field_map is not an object")
    out: dict[str, str] = {}
    for key, value in raw.items():
        k = str(key).strip()
        if k not in FIELD_KEYS:
            raise AdapterError(f"field_map key {k!r} is not a canonical field")
        if value is None or str(value).strip() in ("", "null", "None"):
            continue
        col = _match_column(value, columns)
        if not col:
            raise AdapterError(
                f"field_map[{k}] names column {value!r}, which is not in the "
                f"sampled table — invented column")
        out[k] = col
    for req in REQUIRED_FIELDS:
        if req not in out:
            raise AdapterError(f"field_map must map {req}")
    return out


def _norm_capabilities(raw: Any, field_map: dict[str, str]
                       ) -> dict[str, dict[str, bool]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AdapterError("capabilities is not an object")
    out: dict[str, dict[str, bool]] = {}
    for key, slot in raw.items():
        k = str(key).strip()
        if k not in CAPABILITY_KEYS:
            raise AdapterError(f"capability {k!r} is unknown to the router")
        if not isinstance(slot, dict):
            raise AdapterError(f"capability {k!r} is not an object")
        extra = set(slot) - {"present"}
        if extra:
            raise AdapterError(
                f"capability {k!r} carries {sorted(extra)} — only 'present' "
                f"is allowed; the validator MEASURES fill_rate")
        present = slot.get("present")
        if not isinstance(present, bool):
            raise AdapterError(f"capability {k!r}: present must be true/false")
        if present and k not in field_map:
            raise AdapterError(
                f"capability {k!r} is present:true but field_map maps no "
                f"column for it")
        out[k] = {"present": present}
    return out


def _norm_trade_mapping(raw: Any, known: dict[str, str] | None
                        ) -> dict[str, str]:
    """Source trade codes → canonical slugs.

    When the source's codes are ALREADY known (``known`` — CSLB_CODE_MAP
    for the CSLB portal), the known map is the one that gets stored: the
    AI may reply with a subset of it (a pilot targets one classification),
    but every code it does write must agree with the known one. An
    unknown code or a different slug is invention and is rejected.
    """
    if known:
        if raw:
            for code, slug in raw.items():
                if known.get(str(code).strip()) != str(slug).strip():
                    raise AdapterError(
                        f"trade_mapping[{code!r}] = {slug!r} disagrees with "
                        f"this source's known codes — the platform's map "
                        f"wins (never invent CSLB codes)")
        return dict(known)
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        raise AdapterError("trade_mapping is not an object")
    out: dict[str, str] = {}
    for code, slug in raw.items():
        s = str(slug).strip()
        if s not in CANONICAL_TRADES:
            raise AdapterError(f"trade_mapping[{code!r}] = {s!r} is not a "
                               f"canonical trade")
        out[str(code).strip()] = s
    return out


def _norm_fetch(raw: Any, form_facts: dict[str, Any] | None,
                source_url: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AdapterError("fetch is not an object")
    method = str(raw.get("method", "GET")).strip().upper()
    if method not in METHODS:
        raise AdapterError(f"fetch.method {method!r} is not runnable")
    url = str(raw.get("url", "")).strip()
    if not url.startswith("http"):
        raise AdapterError(f"fetch.url {url!r} is not an http(s) URL")
    if source_url and urlparse(url).netloc != urlparse(source_url).netloc:
        raise AdapterError(
            f"fetch.url host {urlparse(url).netloc!r} is not the registry's "
            f"{urlparse(source_url).netloc!r} — invented host")
    fmt = str(raw.get("format", "")).strip().lower()
    if fmt not in FORMATS:
        raise AdapterError(f"fetch.format {fmt!r} is unreadable")
    shape = raw.get("file_shape")
    shape = dict(shape) if isinstance(shape, dict) else {}
    if fmt.startswith("zip/") and not str(shape.get("inner_path", "")).strip():
        raise AdapterError(f"fetch.format {fmt!r} needs file_shape.inner_path")
    if method == "POST":
        form = raw.get("form")
        if not isinstance(form, dict) or not str(form.get("select_field", "")) \
                or not str(form.get("code", "")):
            raise AdapterError("a POST fetch needs form.select_field + form.code")
        _check_form_against_page(form, form_facts)
    out = {
        "method": method,
        "url": url,
        "format": fmt,
        "refresh": str(raw.get("refresh", "")).strip(),
        "if_modified": raw.get("if_modified") or {},
        "file_shape": shape,
    }
    if method == "POST":
        out["form"] = dict(raw["form"])
    return out


def _check_form_against_page(form: dict[str, Any],
                             form_facts: dict[str, Any] | None) -> None:
    """A POST spec may only use controls the live page actually has —
    same rule as field_map columns: real facts, never invented ones."""
    if not form_facts:
        return
    selects = form_facts.get("selects") or {}
    select_field = str(form.get("select_field", "")).strip()
    if selects and select_field not in selects:
        raise AdapterError(
            f"form.select_field {select_field!r} is not a select on the page "
            f"(page has {sorted(selects)[:5]})")
    code = str(form.get("code", "")).strip()
    options = selects.get(select_field) or []
    if options and code not in options:
        raise AdapterError(
            f"form.code {code!r} is not a value {select_field!r} accepts "
            f"(page offers {options[:5]}…)")
    submits = [s.get("name", "") for s in (form_facts.get("submits") or [])]
    submit_field = str(form.get("submit_field", "")).strip()
    if submits and submit_field and submit_field not in submits:
        raise AdapterError(
            f"form.submit_field {submit_field!r} is not a button on the page "
            f"(page has {submits[:5]})")


def validate_contract(contract: dict[str, Any], *, columns: list[str],
                      access_path: str,
                      source_url: str = "",
                      form_facts: dict[str, Any] | None = None,
                      known_trade_mapping: dict[str, str] | None = None
                      ) -> dict[str, Any]:
    """The hard gate between an AI reply and the registry (§5).

    Returns the normalized contract; raises :class:`AdapterError` with the
    honest reason (which is also what the retry prompt carries).
    """
    if access_path not in PROBER_PATHS:
        raise AdapterError(f"access_path {access_path!r} is not a prober path")
    field_map = _norm_field_map(contract.get("field_map"), columns)
    est = contract.get("estimated_rows")
    if isinstance(est, bool) or not isinstance(est, int) or est <= 0:
        raise AdapterError(f"estimated_rows {est!r} is not a positive integer")
    return {
        "access_path": access_path,
        "fetch": _norm_fetch(contract.get("fetch"), form_facts, source_url),
        "field_map": field_map,
        "capabilities": _norm_capabilities(contract.get("capabilities"),
                                           field_map),
        "trade_mapping": _norm_trade_mapping(contract.get("trade_mapping"),
                                             known_trade_mapping),
        "estimated_rows": est,
    }


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------

def write_adapter(store: ScoutStore, source_id: str, *,
                  columns: list[str],
                  sample_rows: list[dict[str, Any]],
                  access_path: str,
                  ai_ask: Callable[[str], str],
                  source_url: str = "",
                  form_facts: dict[str, Any] | None = None,
                  known_trade_mapping: dict[str, str] | None = None,
                  state: str = "") -> dict[str, Any]:
    """Ask the AI for one contract, validate it, store it, then move the
    lifecycle probing → adapter_draft.

    The AI is called at most :data:`MAX_ATTEMPTS` times: the second
    attempt carries the first rejection's reason. Failure raises
    :class:`AdapterError` and writes NOTHING — a source with a bad
    adapter stays in probing, visible, never silently dropped.
    """
    row = store.get(source_id)
    if row is None:
        raise ValueError(f"unknown source_id: {source_id!r}")
    if not columns:
        raise AdapterError("no columns sampled — nothing to write an adapter on")

    prompt = build_prompt(
        source_id=source_id, state=state, access_path=access_path,
        columns=columns, sample_rows=sample_rows, source_url=source_url,
        form_facts=form_facts, known_trade_mapping=known_trade_mapping)

    reason = ""
    contract: dict[str, Any] | None = None
    for attempt in range(MAX_ATTEMPTS):
        ask = prompt if not reason else (
            f"{prompt}\n\nYour previous reply was REJECTED: {reason}\n"
            f"Return a corrected JSON object.")
        try:
            reply = ai_ask(ask)
        except Exception as exc:  # noqa: BLE001 — the reason is the value
            raise AdapterError(
                f"AI call failed ({type(exc).__name__}): {exc}") from exc
        try:
            contract = validate_contract(
                parse_reply(reply), columns=columns, access_path=access_path,
                source_url=source_url, form_facts=form_facts,
                known_trade_mapping=known_trade_mapping)
            reason = ""
            break
        except AdapterError as exc:
            reason = str(exc)
            contract = None
    if contract is None:
        raise AdapterError(f"adapter rejected after {MAX_ATTEMPTS} attempts: "
                           f"{reason}")

    store.record_adapter(
        source_id,
        fetch_spec=contract["fetch"],
        field_map=contract["field_map"],
        trade_mapping=contract["trade_mapping"],
        capabilities=contract["capabilities"])
    if store.get(source_id)["status"] == STATUS_PROBING:
        store.mark_adapter_draft(
            source_id,
            reason=f"adapter written via {access_path} "
                   f"({len(columns)} columns sampled)")
    out = store.get(source_id)
    assert out is not None
    return {"contract": contract, "row": out}