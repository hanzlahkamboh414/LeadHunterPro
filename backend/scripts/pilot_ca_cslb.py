"""CA CSLB pilot — Phase 3's exit criterion, run LIVE (coverage_engine_v2.md §12).

One source, end to end, no shortcuts: seed the registry row → prove the
portal is reachable → read the form's REAL controls off the page → fetch
a real classification export → let the AI write the adapter (validated
against those real columns and controls) → 500-row dry run through the
Validator's gates → probation trials → promote.

Everything here is the real pipeline; the only pilot-specific code is
the orchestration and the sampling fetch (the AI needs bytes to look at
before it can write a fetch spec — its OWN spec is what the dry run
executes, so a wrong spec fails here, not in production).

Run from backend/:

    python scripts/pilot_ca_cslb.py                    # B-2, 5 trials
    python scripts/pilot_ca_cslb.py --trials 1 --delay-secs 0   # quick pass

CSLB rate-limits rapid sessions (504 after ~3 in 10 minutes), so trials
are spaced by --delay-secs. Every step is written to
output/pilot_ca_cslb_<timestamp>.json — the run is auditable after the
fact, and nothing is ever reported as passed without that record.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The console on Windows defaults to cp1252 and dies on the arrows/em
#: dashes these logs use (and on any reason string that carries one).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover - old interpreters
    pass

from app.phones.cslb import CSLB_CODE_MAP, PORTAL_URL  # noqa: E402
from app.source_scout.adapter_writer import (  # noqa: E402
    AdapterError,
    adapter_of,
    write_adapter,
)
from app.source_scout.boards import seed_registry  # noqa: E402
from app.source_scout.bulk_fetch import fetch_bytes, form_facts  # noqa: E402
from app.source_scout.dryrun import dry_run, probation_check_v2  # noqa: E402
from app.source_scout.prober import KIND_OK, fetch_probe  # noqa: E402
from app.source_scout.probation import default_ai_ask  # noqa: E402
from app.source_scout.store import (  # noqa: E402
    STATUS_DEAD,
    STATUS_PROMOTED,
    STATUS_PROBATION,
    STATUS_UNTRIED,
    ScoutStore,
    default_db_path,
    get_store,
)
from app.source_scout.tabular import read_rows  # noqa: E402

#: The registry id the seed CSV produces for California.
SOURCE_ID = "state_ca_board"

#: The access path this pilot exercises: CSLB's export comes from a form
#: POST, so the adapter's spec is an html_form one.
ACCESS_PATH = "html_form"

#: Trade codes the adapter may map — the production map, never a new one.
KNOWN_TRADES = {code: slug for code, (slug, _label) in CSLB_CODE_MAP.items()}

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BACKEND_ROOT, "output")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _log(step: str, detail: str = "") -> None:
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {step}"
          + (f" — {detail}" if detail else ""), flush=True)


def _probe_spec(code: str, facts: dict[str, Any]) -> dict[str, Any]:
    """The pilot's own sampling spec, built from the page's real controls."""
    select_field = next(iter(facts["selects"]))
    submit = next((s for s in facts["submits"] if s.get("name")), None)
    form: dict[str, Any] = {"select_field": select_field, "code": code}
    if submit:
        form["submit_field"] = submit["name"]
        form["submit_value"] = submit.get("value", "Download")
    return {"method": "POST", "url": PORTAL_URL, "format": "xlsx",
            "form": form}


def _reachable(store: ScoutStore, evidence: dict[str, Any]) -> bool:
    """Prove the seeded base_url answers, and record the path we will use.

    The prober's own walk tries the seed's ``known_access`` hint first —
    for CSLB that hint is a guessed bulk-file URL that 404s, and a 404 on
    a GUESS must never mark the source dead. So the pilot probes the
    seeded base_url itself and records the path it actually exercises.
    """
    row = store.get(SOURCE_ID)
    assert row is not None
    status = row["status"]
    if status == STATUS_DEAD:
        _log("probe", "source is DEAD — aborting")
        evidence["aborted"] = "source status is dead"
        return False
    if status == STATUS_UNTRIED:
        store.start_probing(SOURCE_ID)
    probe = fetch_probe(row["base_url"] or PORTAL_URL)
    _log("probe", f"{row['base_url']} -> {probe.kind} ({probe.reason})")
    evidence["probe"] = {"url": row["base_url"], "kind": probe.kind,
                         "status_code": probe.status_code,
                         "reason": probe.reason}
    if probe.kind != KIND_OK:
        evidence["aborted"] = f"portal unreachable: {probe.reason}"
        return False
    store.record_probe_success(SOURCE_ID, ACCESS_PATH)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--classification", default="B-2",
                    help="the CSLB classification code to pilot (default B-2)")
    ap.add_argument("--trials", type=int, default=5,
                    help="probation trials to run (promote threshold is 5)")
    ap.add_argument("--delay-secs", type=float, default=90.0,
                    help="spacing between live fetches (CSLB rate-limits)")
    ap.add_argument("--limit", type=int, default=500,
                    help="dry-run sample size")
    ap.add_argument("--db", default="",
                    help="scout registry DB (default: the production registry)")
    args = ap.parse_args()

    code = args.classification.strip()
    store = ScoutStore(db_path=args.db) if args.db else get_store()
    db_path = args.db or default_db_path()
    evidence: dict[str, Any] = {
        "pilot": "ca_cslb_bulk", "started_at": _now(), "db": str(db_path),
        "source_id": SOURCE_ID, "classification": code,
        "classification_label": CSLB_CODE_MAP.get(code, ("", ""))[1],
        "trade_slug": CSLB_CODE_MAP.get(code, ("", ""))[0],
        "steps": [],
    }
    _log("start", f"db={db_path} classification={code}")

    # -- 1. the registry row (seeds are committed CSVs, never AI) -----------
    counts = seed_registry(store)
    row = store.get(SOURCE_ID)
    if row is None:
        raise SystemExit(f"seed did not produce {SOURCE_ID!r}")
    evidence["steps"].append({"step": "seed", "counts": counts,
                              "status": row["status"],
                              "base_url": row["base_url"]})
    _log("seed", f"boards={counts['boards']} emails={counts['emails']} "
                 f"{SOURCE_ID} status={row['status']}")

    # -- 2. reachability ---------------------------------------------------
    if not _reachable(store, evidence):
        return _finish(evidence, promoted=False)

    # -- 3. the page's real form controls ----------------------------------
    try:
        facts = form_facts(PORTAL_URL)
    except Exception as exc:  # noqa: BLE001 — an unreachable form is the report
        _log("form_facts", f"FAILED: {exc}")
        evidence["aborted"] = f"form_facts failed: {exc}"
        return _finish(evidence, promoted=False)
    options = facts["selects"].get(next(iter(facts["selects"]), ""), [])
    evidence["form_facts"] = {"url": facts["url"], "hidden": facts["hidden"],
                              "selects": {k: v[:5] for k, v in
                                          facts["selects"].items()},
                              "submits": facts["submits"]}
    _log("form_facts", f"{len(facts['selects'])} select(s), "
                       f"{len(facts['submits'])} button(s), "
                       f"{len(options)} options in the first select")
    if code not in options:
        _log("form_facts", f"{code!r} is not offered by the page — aborting")
        evidence["aborted"] = f"{code!r} not among {options[:20]}"
        return _finish(evidence, promoted=False)

    # -- 4. the sampling fetch (real bytes, real download) -----------------
    spec = _probe_spec(code, facts)
    try:
        data = fetch_bytes(spec)
        columns, rows = read_rows(data, fmt="xlsx",
                                  file_shape={"header_row": 1})
    except Exception as exc:  # noqa: BLE001 — report, never guess
        _log("sample", f"FAILED: {exc}")
        evidence["aborted"] = f"sampling fetch failed: {exc}"
        return _finish(evidence, promoted=False)
    os.makedirs(OUT_DIR, exist_ok=True)
    raw_path = os.path.join(OUT_DIR, f"cslb_pilot_{code}.xlsx")
    with open(raw_path, "wb") as fh:
        fh.write(data)
    evidence["sample"] = {"bytes": len(data), "columns": columns,
                          "rows": len(rows), "raw_file": raw_path}
    _log("sample", f"{len(data)} bytes, {len(columns)} columns, "
                   f"{len(rows)} rows -> {raw_path}")

    # -- 5. the AI writes the adapter (validated, or nothing is stored) ----
    try:
        written = write_adapter(
            store, SOURCE_ID, columns=columns, sample_rows=rows,
            access_path=ACCESS_PATH, ai_ask=default_ai_ask(),
            source_url=row["base_url"], form_facts=facts,
            known_trade_mapping=KNOWN_TRADES, state=row.get("state", "CA"))
    except AdapterError as exc:
        _log("adapter", f"REJECTED: {exc}")
        evidence["adapter"] = {"ok": False, "reason": str(exc)}
        return _finish(evidence, promoted=False)
    contract = written["contract"]
    evidence["adapter"] = {"ok": True, "contract": contract,
                           "status": written["row"]["status"]}
    _log("adapter", f"written: {contract['fetch']['method']} "
                    f"{contract['fetch']['url']} → "
                    f"{contract['fetch']['format']}, "
                    f"{len(contract['field_map'])} fields mapped")

    # -- 6. the 500-row dry run through the Validator ----------------------
    _sleep(args.delay_secs)
    run = dry_run(store, SOURCE_ID, limit=args.limit)
    result = run["result"]
    evidence["dry_run"] = {
        "limit": args.limit, "sampled": len(run["rows"]),
        "measured_rows": result.estimated_rows, "passed": result.passed,
        "gates": result.gates, "capabilities": result.capabilities,
        "reason": result.summary_reason, "status": run["row"]["status"],
    }
    _log("dry_run", f"passed={result.passed} "
                    f"rows={result.estimated_rows} "
                    f"status={run['row']['status']} "
                    f"{result.summary_reason}")
    if not result.passed:
        return _finish(evidence, promoted=None)

    # -- 7. probation trials (fresh live fetch each time) ------------------
    ai = default_ai_ask()
    trials: list[dict[str, Any]] = []
    for i in range(max(1, args.trials)):
        _sleep(args.delay_secs)
        out = probation_check_v2(store, SOURCE_ID, ai_ask=ai,
                                 limit=args.limit)
        trials.append({"verdict": out["verdict"], "detail": out["detail"],
                       "agnes": out["agnes_reason"], "status": out["status"],
                       "score": list(out["score"])})
        _log(f"trial {i + 1}/{args.trials}",
             f"verdict={out['verdict']} score={out['score'][0]}/"
             f"{out['score'][1]} status={out['status']}")
        if out["status"] != STATUS_PROBATION:
            break
    evidence["trials"] = trials

    final = store.get(SOURCE_ID)
    promoted = bool(final and final["status"] == STATUS_PROMOTED)
    if promoted:
        stored = adapter_of(final)
        evidence["promotion"] = {"capabilities": stored["capabilities"],
                                 "estimated_rows": final["estimated_rows"],
                                 "access_path": final["access_path"],
                                 "promoted_at": final.get("promoted_at", "")}
    return _finish(evidence, promoted=promoted)


def _sleep(secs: float) -> None:
    if secs > 0:
        _log("wait", f"{secs:.0f}s (CSLB rate limit)")
        time.sleep(secs)


def _finish(evidence: dict[str, Any], *, promoted: bool | None) -> int:
    evidence["finished_at"] = _now()
    evidence["promoted"] = bool(promoted)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(
        OUT_DIR, f"pilot_ca_cslb_{evidence['started_at'].replace(':', '')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(evidence, fh, indent=1, default=str)
    _log("evidence", path)
    _log("result", "PROMOTED" if promoted else
         ("ABORTED" if evidence.get("aborted") else "NOT PROMOTED"))
    return 0 if promoted else 1


if __name__ == "__main__":
    sys.exit(main())
