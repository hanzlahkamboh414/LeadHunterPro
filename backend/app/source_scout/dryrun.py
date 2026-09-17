"""Dry run + V2 probation — the source proves itself (§6, §7, §12 Phase 3).

A source that has an adapter is still a hypothesis. This module runs the
real thing end to end, always against live bytes:

    fetch the stored fetch_spec  →  read it under its file_shape
    →  500-row dry run through the Validator's gates  →  probation trials
    →  promote / retire (the V1 thresholds, reused: 5 trials, ≥70%)

Nothing here is AI except the optional final quality judgement, and that
verdict is recorded only (a mechanical failure is never overridden by an
opinion). Every failure path stores an honest reason and leaves the
source visible — no silent drops, no fabricated "live" results.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.source_scout.adapter_writer import adapter_of
from app.source_scout.bulk_fetch import BulkFetchError, fetch_bytes
from app.source_scout.probation import (
    MAX_ROWS_TO_SHOW_AI,
    STAGE_PROBATION,
    advance_probation,
    default_ai_ask,
    parse_agnes_reply,
)
from app.source_scout.store import (
    STATUS_PROBATION,
    ScoutStore,
    get_store,
)
from app.source_scout.tabular import TabularError, read_rows
from app.source_scout.validator import ValidationResult, apply_validation, validate

#: The §6 exit criterion: a real N-row dry run.
DRY_RUN_ROWS = 500

#: Capability keys whose mapped column means "we can serve this field".
_CAPABILITY_KEYS = ("phone", "email", "address", "city", "license_no",
                    "trade", "status")

#: Bytes/text of a failed fetch, kept for the reason only.
_MAX_REASON_CHARS = 300


def _claimed_capabilities(adapter: dict[str, Any]) -> dict[str, Any]:
    """The adapter's present-flags — derived from its field_map when it
    claims none.

    An adapter that maps a column but claims nothing would be promoted
    with no capability at all, and the router could never bind demand to
    it (a silent no-op promotion). Deriving the claim from the field_map
    instead hands the question to the Validator, whose per-capability
    measurement downgrades anything below 30% (§6) — measurement first.
    """
    caps = dict(adapter.get("capabilities") or {})
    if caps:
        return caps
    return {k: {"present": True} for k in _CAPABILITY_KEYS
            if k in (adapter.get("field_map") or {})}


def _total_rows(data: bytes, fmt: str, file_shape: dict[str, Any],
                sample_size: int, limit: int) -> int:
    """The re-measured file size (§6): the AI's estimate is never stored
    as a measurement. A capped sample that did not fill the cap IS the
    whole file; otherwise re-read it fully (best effort)."""
    if sample_size < limit:
        return sample_size
    try:
        _, rows = read_rows(data, fmt=fmt, file_shape=file_shape, max_rows=0)
        return len(rows)
    except TabularError:
        return sample_size


def _parse_failure(store: ScoutStore, source_id: str, reason: str,
                   adapter: dict[str, Any], *, auto_advance: bool
                   ) -> dict[str, Any]:
    """Gate 0 did not even get typed rows — record it like any other gate
    failure so the lifecycle (and the retry logic) sees it uniformly."""
    result = ValidationResult(
        source_id, False,
        {"parse_feasibility": {"passed": False, "reason": reason},
         "fetch": {"passed": False, "reason": reason}},
        _claimed_capabilities(adapter), 0,
        f"gate 0 parse_feasibility: {reason}")
    row = apply_validation(store, source_id, result, auto_advance=auto_advance)
    return {"source_id": source_id, "columns": [], "rows": [],
            "result": result, "row": row, "fetched_bytes": 0}


def dry_run(store: ScoutStore, source_id: str, *,
            fetch_fn: Callable[..., bytes] = fetch_bytes,
            transport: Any = None,
            limit: int = DRY_RUN_ROWS,
            auto_advance: bool = True) -> dict[str, Any]:
    """Fetch → read → 500-row dry run through the Validator's gates.

    ``auto_advance`` mirrors the validator: True moves adapter_draft →
    probation on a pass (or bounces it back on a fail); False is the
    probation re-check, which only records measurements.
    """
    row = store.get(source_id)
    if row is None:
        raise ValueError(f"unknown source_id: {source_id!r}")
    adapter = adapter_of(row)
    spec = adapter["fetch_spec"]
    if not spec:
        raise ValueError(f"{source_id} has no stored adapter to dry-run")
    fmt = str(spec.get("format", ""))
    file_shape = spec.get("file_shape") or {}

    try:
        data = fetch_fn(spec, transport=transport)
        columns, rows = read_rows(data, fmt=fmt, file_shape=file_shape,
                                  max_rows=limit)
    except (BulkFetchError, TabularError) as exc:
        return _parse_failure(store, source_id, str(exc)[:_MAX_REASON_CHARS],
                              adapter, auto_advance=auto_advance)

    result = validate(
        source_id, rows, field_map=adapter["field_map"],
        adapter_capabilities=_claimed_capabilities(adapter),
        seed_state=str(row.get("state", "")),
        total_rows=_total_rows(data, fmt, file_shape, len(rows), limit))
    applied = apply_validation(store, source_id, result,
                              auto_advance=auto_advance)
    return {"source_id": source_id, "columns": columns, "rows": rows,
            "result": result, "row": applied, "fetched_bytes": len(data)}


def _verdict_detail(run: dict[str, Any]) -> str:
    """One honest line: the gates that failed, or the measurement that
    passed. This is what the verdict log keeps."""
    result: ValidationResult = run["result"]
    if not result.passed:
        return result.summary_reason[:_MAX_REASON_CHARS]
    caps = ", ".join(
        f"{k}={v.get('fill_rate', 0):.0%}"
        for k, v in sorted(result.capabilities.items())
        if v.get("present"))
    return (f"{len(run['rows'])} rows sampled, "
            f"{result.estimated_rows} rows measured"
            + (f", fill {caps}" if caps else ""))


def judge_rows(ai_ask: Callable[[str], str], *, field_map: dict[str, str],
               columns: list[str],
               rows: list[dict[str, Any]]) -> tuple[bool | None, str]:
    """The one opinion in the V2 loop: are these REAL, usable records?

    Same contract as the V1 probation judgement (PASS/FAIL + one line),
    read by the same parser. An unparseable reply or a dead LLM returns
    ``(None, why)`` — an absent opinion, never a silent pass.
    """
    show = [{k: str(r.get(field_map.get(k, ""), ""))[:60]
             for k in ("company_name", "phone", "trade", "city", "address")
             if field_map.get(k)} for r in rows[:MAX_ROWS_TO_SHOW_AI]]
    prompt = (
        "You judge whether rows from a US contractor-license / business "
        "registry are REAL, USABLE lead data. A usable row names a real "
        "business, carries a phone number a human could dial, and a trade "
        "or license classification. Placeholder values, redacted fields, "
        "test data or a wrong column (a date where a phone should be) make "
        "the rows unusable.\n"
        f"Mapped fields: {json.dumps(field_map, sort_keys=True)}\n"
        f"Source columns: {json.dumps(columns)}\n"
        f"Sample rows:\n{json.dumps(show, indent=1)}\n"
        "Answer exactly PASS or FAIL, then one short sentence why.")
    try:
        reply = ai_ask(prompt)
    except Exception as exc:  # noqa: BLE001 — our AI being down is not the source's fault
        return None, f"LLM call failed: {type(exc).__name__}"
    return parse_agnes_reply(reply)


def probation_check_v2(store: ScoutStore, source_id: str, *,
                       fetch_fn: Callable[..., bytes] = fetch_bytes,
                       transport: Any = None,
                       ai_ask: Callable[[str], str] | None = None,
                       limit: int = DRY_RUN_ROWS) -> dict[str, Any]:
    """One probation trial on a V2 source: a FRESH live fetch → gates →
    optional agnes judgement → verdict → advance_probation.

    The mechanical gates decide. Agnes can only ever add a FAIL (an
    opinion must never rescue a source whose rows do not hold up
    mechanically). Promote/retire thresholds are the existing §7 ones.
    """
    row = store.get(source_id)
    if row is None:
        raise ValueError(f"unknown source_id: {source_id!r}")
    if row["status"] != STATUS_PROBATION:
        raise ValueError(
            f"probation_check_v2 needs a source in probation, got "
            f"{row['status']!r}")

    run = dry_run(store, source_id, fetch_fn=fetch_fn, transport=transport,
                  limit=limit, auto_advance=False)
    result: ValidationResult = run["result"]
    verdict = result.passed
    detail = _verdict_detail(run)
    agnes_reason = ""

    if verdict and ai_ask is not None and run["rows"]:
        adapter = adapter_of(row)
        ok, agnes_reason = judge_rows(
            ai_ask, field_map=adapter["field_map"], columns=run["columns"],
            rows=run["rows"])
        if ok is False:
            verdict = False
            detail = f"agnes FAIL: {agnes_reason} | {detail}"

    store.record_verdict(source_id, STAGE_PROBATION, verdict,
                         detail[:_MAX_REASON_CHARS])
    status = advance_probation(store, source_id)
    return {"source_id": source_id, "verdict": verdict, "detail": detail,
            "agnes_reason": agnes_reason, "result": result, "status": status,
            "score": store.probation_score(source_id)}


def probation_pass_v2(store: ScoutStore | None = None, *,
                      fetch_fn: Callable[..., bytes] = fetch_bytes,
                      transport: Any = None,
                      ai_ask: Callable[[str], str] | None = None,
                      limit: int = DRY_RUN_ROWS) -> dict[str, Any]:
    """One trial for every V2 source currently in probation.

    The caller spaces the passes (the pilot sleeps between trials): the
    pass RATE only means something if trials are independent fetches
    over time.
    """
    store = store or get_store()
    checked: list[str] = []
    promoted: list[str] = []
    retired: list[str] = []
    skipped: dict[str, str] = {}

    for cand in store.list_status(STATUS_PROBATION, limit=1000):
        source_id = cand["source_id"]
        row = store.get(source_id)
        if row is None or not (row.get("fetch_spec") or "").strip("{} "):
            skipped[source_id] = "no V2 adapter (fetch_spec empty)"
            continue
        try:
            out = probation_check_v2(store, source_id, fetch_fn=fetch_fn,
                                     transport=transport, ai_ask=ai_ask,
                                     limit=limit)
        except ValueError as exc:
            skipped[source_id] = str(exc)
            continue
        checked.append(source_id)
        if out["status"] == "promoted":
            promoted.append(source_id)
        elif out["status"] == "retired":
            retired.append(source_id)

    return {"checked": checked, "promoted": promoted, "retired": retired,
            "skipped": skipped}
