"""Validator — the hard-gate engine on a real dry-run sample (§6).

Plain Python, no AI. The adapter carries present-flags ONLY; the
validator MEASURES them — that is where fill_rate (the quota-formula
number) and the corrected estimated_rows come from. Gate 3 is
per-capability (a weak capability is downgraded, never the whole
source); gates 0, 1, 2, 4, 5 are source-level.

Deferred to Phase 5 (recorded here so it is not forgotten, §6): phone
format validation (libphonenumber-style), role-email down-weighting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.source_scout.store import (
    STATUS_ADAPTER_DRAFT,
    ScoutStore,
)

#: gate 1 — a source under this row count is empty, not "small".
MIN_ROWS = 50
#: gate 2 — the canonical name column must be filled on ≥95% of rows,
#: else the field_map is wrong (schema_mismatch).
COMPANY_NAME_FILL = 0.95
#: gate 3 — a claimed capability below this measured fill is downgraded
#: (present: false), never source-fatal.
CAPABILITY_FILL = 0.30
#: gate 5 — above this duplicate rate the pagination/file dedupe is broken.
MAX_DUP_RATE = 0.20

#: Gate 3 never decides the source; the source gates in their check order.
_SOURCE_GATES = ("parse_feasibility", "rows_returned", "company_name",
                 "jurisdiction", "duplicate_rate")


@dataclass
class ValidationResult:
    """The validator's verdict + the measurements the registry stores."""

    source_id: str
    passed: bool
    gates: dict[str, dict[str, Any]] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    estimated_rows: int = 0
    summary_reason: str = ""


def _fill_rate(sample: list[dict[str, Any]], column: str) -> float:
    """Non-empty (stripped) values as a share of ALL rows — a 200-row
    sample with 40 empty phones measures 0.80, not 1.00."""
    if not sample:
        return 0.0
    filled = sum(
        1 for r in sample if str(r.get(column, "")).strip())
    return filled / len(sample)


def _dup_rate(sample: list[dict[str, Any]], name_col: str,
              state_col: str) -> float:
    """Exact-key duplicates over the sample — the record_hash analog
    (§10: a file has no cursor, the hash is the only honest one)."""
    seen: set[tuple[str, str]] = set()
    dups = 0
    for r in sample:
        key = (str(r.get(name_col, "")).strip().lower(),
               str(r.get(state_col, "")).strip())
        if key in seen:
            dups += 1
        else:
            seen.add(key)
    return dups / len(sample) if sample else 0.0


def validate(source_id: str, sample: list[dict[str, Any]], *,
             field_map: dict[str, str],
             adapter_capabilities: dict[str, Any],
             seed_state: str = "",
             total_rows: int | None = None) -> ValidationResult:
    """Gate 0 + the 5 semantic gates over one dry-run sample.

    ``field_map`` is the adapter's canonical→source-column map; the
    claimed (present-only) capabilities are the adapter's; ``seed_state``
    is the registry's authoritative jurisdiction (gate 4 — the ID-phantom
    killer). ``total_rows`` is the re-measured count (count(*)/line
    count); when absent the sample size stands in — the AI's estimate is
    never a measurement.
    """
    gates: dict[str, dict[str, Any]] = {}
    capabilities = {
        cap: dict(slot) for cap, slot in adapter_capabilities.items()
        if isinstance(slot, dict)
    }

    # -- gate 0 — parse feasibility (the fetch actually yields typed rows)
    ok0 = bool(sample) and all(isinstance(r, dict) for r in sample)
    gates["parse_feasibility"] = {
        "passed": ok0,
        "reason": "" if ok0 else "no typed rows yielded (parse failed)",
    }
    if not ok0:
        return ValidationResult(
            source_id, False, gates, capabilities, 0,
            "gate 0 parse_feasibility: no typed rows")

    n = len(sample)

    # -- gate 1 — rows returned
    ok1 = n >= MIN_ROWS
    gates["rows_returned"] = {
        "passed": ok1,
        "reason": ("" if ok1
                   else f"only {n} rows (< {MIN_ROWS}) — empty_source"),
    }

    # -- gate 2 — company_name fill (the field_map is wrong if this fails)
    name_col = field_map.get("company_name", "")
    if not name_col:
        ok2, reason2 = False, "field_map lacks company_name — schema_mismatch"
    else:
        rate2 = _fill_rate(sample, name_col)
        ok2 = rate2 >= COMPANY_NAME_FILL
        reason2 = ("" if ok2
                   else f"company_name fill {rate2:.1%} < "
                        f"{COMPANY_NAME_FILL:.0%} — field_map wrong")
    gates["company_name"] = {"passed": ok2, "reason": reason2}

    # -- gate 3 — per-CLAIMED capability: measure, downgrade, never reject
    downgraded: list[str] = []
    for cap, slot in capabilities.items():
        if not slot.get("present"):
            continue  # unclaimed → nothing to measure, left untouched
        col = field_map.get(cap, "")
        rate = _fill_rate(sample, col) if col else 0.0
        slot["fill_rate"] = round(rate, 4)
        if rate < CAPABILITY_FILL:
            slot["present"] = False  # downgraded, stored as measured
            downgraded.append(cap)
    gates["capability_fill"] = {
        "passed": True,  # gate 3 is per-capability, never source-fatal
        "downgraded": sorted(downgraded),
    }

    # -- gate 4 — state_field ≡ seed state (kills ID phantoms)
    state_col = field_map.get("state_field", "")
    if not seed_state:
        # email tier: no seeded jurisdiction anchor — not enforceable
        ok4, reason4 = True, "no seed jurisdiction (email tier) — skipped"
    elif not state_col:
        ok4, reason4 = False, "field_map lacks state_field — jurisdiction_error"
    else:
        bad = sorted({
            str(r.get(state_col, "")).strip()
            for r in sample
            if str(r.get(state_col, "")).strip()
            and str(r.get(state_col, "")).strip() != seed_state
        })
        ok4 = not bad
        reason4 = ("" if ok4
                   else f"state_field rows {bad[:5]} ≠ seed {seed_state!r} — "
                        f"jurisdiction_error")
    gates["jurisdiction"] = {"passed": ok4, "reason": reason4}

    # -- gate 5 — duplicate rate
    dup = _dup_rate(sample, name_col, state_col) if name_col else 1.0
    ok5 = dup < MAX_DUP_RATE
    gates["duplicate_rate"] = {
        "passed": ok5,
        "reason": ("" if ok5
                   else f"dup rate {dup:.1%} ≥ {MAX_DUP_RATE:.0%} — "
                        f"pagination/file dedupe broken"),
        "dup_rate": round(dup, 4),
    }

    passed = all(gates[g]["passed"] for g in _SOURCE_GATES)
    reason = next((gates[g]["reason"] for g in _SOURCE_GATES
                   if not gates[g]["passed"]), "")
    return ValidationResult(
        source_id, passed, gates, capabilities,
        int(total_rows) if total_rows is not None else n,
        "" if passed else f"gate fail: {reason}")


def apply_validation(store: ScoutStore, source_id: str,
                     result: ValidationResult, *,
                     auto_advance: bool = True) -> dict[str, Any]:
    """Persist the validator's measurements, then move the lifecycle (§7).

    pass → adapter_draft → probation (the N-row dry run); fail → back to
    probing for a fresh adapter (schema_mismatch — never silent
    retirement). ``auto_advance=False`` leaves status alone (re-checks
    inside probation).
    """
    store.update_validation(
        source_id,
        capabilities=result.capabilities,
        estimated_rows=result.estimated_rows,
        gate_fail_reason=result.summary_reason)
    row = store.get(source_id)
    assert row is not None
    if auto_advance and row["status"] == STATUS_ADAPTER_DRAFT:
        if result.passed:
            return store.enter_probation(source_id)
        return store.rework_adapter(source_id, result.summary_reason)
    return row


def run_validation(store: ScoutStore, source_id: str,
                   sample: list[dict[str, Any]], *,
                   field_map: dict[str, str],
                   adapter_capabilities: dict[str, Any],
                   seed_state: str = "",
                   total_rows: int | None = None,
                   auto_advance: bool = True
                   ) -> tuple[dict[str, Any], ValidationResult]:
    """One-shot: validate() then apply_validation(). The pipeline seam
    Phase 3's probation runner calls with the dry-run sample."""
    result = validate(
        source_id, sample, field_map=field_map,
        adapter_capabilities=adapter_capabilities,
        seed_state=seed_state, total_rows=total_rows)
    row = apply_validation(store, source_id, result,
                           auto_advance=auto_advance)
    return row, result