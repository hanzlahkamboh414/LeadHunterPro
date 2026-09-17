"""dryrun tests — the 500-row dry run + V2 probation trials (§6, §7).

Fake bytes stand in for the live download (a real zip-built xlsx and a
CSV built in-test), so the whole path is exercised without a network:
stored adapter → fetch_spec → read under file_shape → validator gates →
probation verdicts → promote/retire on the reused V1 thresholds.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.source_scout.dryrun import (
    DRY_RUN_ROWS,
    dry_run,
    judge_rows,
    probation_check_v2,
    probation_pass_v2,
)
from app.source_scout.probation import PROBATION_MIN_TRIALS
from app.source_scout.store import (
    STATUS_ADAPTER_DRAFT,
    STATUS_PROBATION,
    STATUS_PROMOTED,
    ScoutStore,
)

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
COLUMNS = ["BusinessName", "BusinessPhone", "State", "Classification"]

_SPEC = {"method": "GET", "url": "https://www.cslb.ca.gov/master.xlsx",
         "format": "xlsx", "refresh": "monthly",
         "file_shape": {"header_row": 1}}
_FIELD_MAP = {"company_name": "BusinessName", "phone": "BusinessPhone",
              "state_field": "State", "trade": "Classification"}
_CAPS = {"phone": {"present": True}, "email": {"present": False}}


def _store(tmp_path) -> ScoutStore:
    return ScoutStore(db_path=str(tmp_path / "scout.db"))


def _cslb_row_values(i: int) -> list[str]:
    return [f"Acme Builders {i}", f"555-0{i:04d}", "CA", "B"]


def _xlsx_bytes(n: int, *, phone_col: int = 1) -> bytes:
    """A real minimal xlsx with n CSLB-shaped rows."""
    rows = [COLUMNS] + [_cslb_row_values(i) for i in range(n)]
    strings: list[str] = []
    body: list[str] = []
    for r_i, row in enumerate(rows, start=1):
        cells = []
        for c_i, val in enumerate(row):
            col = chr(ord("A") + c_i)
            if val.isdigit():
                cells.append(f'<c r="{col}{r_i}"><v>{val}</v></c>')
            else:
                if val not in strings:
                    strings.append(val)
                cells.append(f'<c r="{col}{r_i}" t="s">'
                             f"<v>{strings.index(val)}</v></c>")
        body.append(f'<row r="{r_i}">{"".join(cells)}</row>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml",
                   f'<sst xmlns="{_NS}">'
                   + "".join(f"<si><t>{s}</t></si>" for s in strings)
                   + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet xmlns="{_NS}"><sheetData>{"".join(body)}'
                   f"</sheetData></worksheet>")
    return buf.getvalue()


def _fetch(data: bytes):
    def fn(spec, transport=None):  # noqa: ARG001 — the stored spec is asserted below
        return data
    return fn


def _seed_with_adapter(store: ScoutStore, source_id="state_ca_board", *,
                       capabilities=None, field_map=None) -> None:
    store.seed_upsert(source_id, seed_domain="phones", state="CA", name="CSLB",
                      base_url="https://www.cslb.ca.gov",
                      seed_meta={"trade_scope": "all_trades", "priority_rank": 1})
    store.start_probing(source_id)
    store.record_adapter(source_id, fetch_spec=_SPEC,
                         field_map=field_map or _FIELD_MAP,
                         trade_mapping={"B": "gc"},
                         capabilities=capabilities if capabilities is not None
                         else _CAPS)
    store.mark_adapter_draft(source_id)


# ---------------------------------------------------------------------------
# the dry run
# ---------------------------------------------------------------------------

def test_dry_run_passes_and_enters_probation_with_measured_rows(tmp_path):
    store = _store(tmp_path)
    _seed_with_adapter(store)
    out = dry_run(store, "state_ca_board", fetch_fn=_fetch(_xlsx_bytes(600)))

    assert out["result"].passed
    assert out["row"]["status"] == STATUS_PROBATION
    assert out["columns"] == COLUMNS
    assert len(out["rows"]) == DRY_RUN_ROWS  # the §6 dry run size
    assert out["result"].estimated_rows == 600  # re-measured, not the cap
    assert out["fetched_bytes"] > 0


def test_dry_run_gate1_failure_bounces_back_to_probing(tmp_path):
    store = _store(tmp_path)
    _seed_with_adapter(store)
    out = dry_run(store, "state_ca_board", fetch_fn=_fetch(_xlsx_bytes(10)))
    assert not out["result"].passed
    assert "only 10 rows" in out["row"]["gate_fail_reason"]
    assert out["row"]["status"] == "probing"  # schema_mismatch bounce


def test_dry_run_bad_bytes_are_a_gate0_failure_not_a_crash(tmp_path):
    store = _store(tmp_path)
    _seed_with_adapter(store)
    out = dry_run(store, "state_ca_board",
                  fetch_fn=_fetch(b"<html>portal down</html>"))
    assert not out["result"].passed
    assert "gate 0 parse_feasibility" in out["row"]["gate_fail_reason"]
    assert out["row"]["status"] == "probing"


def test_dry_run_fetch_error_is_recorded_honestly(tmp_path):
    store = _store(tmp_path)
    _seed_with_adapter(store)

    def boom(spec, transport=None):
        from app.source_scout.bulk_fetch import BulkFetchError
        raise BulkFetchError("HTTP 503")

    out = dry_run(store, "state_ca_board", fetch_fn=boom)
    assert "HTTP 503" in out["row"]["gate_fail_reason"]
    assert out["row"]["status"] == "probing"


def test_dry_run_derives_capabilities_when_the_adapter_claims_none(tmp_path):
    """An adapter that maps columns but claims nothing must still be
    measured — otherwise it could promote with no capability at all and
    the router could never bind demand to it."""
    store = _store(tmp_path)
    _seed_with_adapter(store, capabilities={})
    out = dry_run(store, "state_ca_board", fetch_fn=_fetch(_xlsx_bytes(60)))
    assert out["result"].passed
    caps = out["result"].capabilities
    assert caps["phone"]["present"] is True      # mapped → claimed → measured
    assert caps["phone"]["fill_rate"] == 1.0
    assert "email" not in caps                   # never mapped, never invented
    assert json.loads(store.get("state_ca_board")["capabilities"]) == caps


def test_dry_run_requires_a_stored_adapter(tmp_path):
    store = _store(tmp_path)
    store.seed_upsert("state_nv_board", seed_domain="phones", state="NV",
                      name="NV", base_url="https://nv.gov")
    with pytest.raises(ValueError, match="no stored adapter"):
        dry_run(store, "state_nv_board", fetch_fn=_fetch(b"x"))
    with pytest.raises(ValueError, match="unknown source_id"):
        dry_run(store, "nope", fetch_fn=_fetch(b"x"))


def test_dry_run_auto_advance_false_keeps_adapter_draft(tmp_path):
    store = _store(tmp_path)
    _seed_with_adapter(store)
    out = dry_run(store, "state_ca_board", fetch_fn=_fetch(_xlsx_bytes(60)),
                  auto_advance=False)
    assert out["result"].passed
    assert out["row"]["status"] == STATUS_ADAPTER_DRAFT


# ---------------------------------------------------------------------------
# probation trials
# ---------------------------------------------------------------------------

def test_probation_trials_promote_at_the_existing_threshold(tmp_path):
    store = _store(tmp_path)
    _seed_with_adapter(store)
    dry_run(store, "state_ca_board", fetch_fn=_fetch(_xlsx_bytes(600)))
    assert store.get("state_ca_board")["status"] == STATUS_PROBATION

    statuses = []
    for _ in range(PROBATION_MIN_TRIALS):
        out = probation_check_v2(store, "state_ca_board",
                                 fetch_fn=_fetch(_xlsx_bytes(600)),
                                 ai_ask=lambda p: "PASS rows look real")
        statuses.append(out["status"])
    assert statuses[:-1] == [STATUS_PROBATION] * (PROBATION_MIN_TRIALS - 1)
    assert statuses[-1] == STATUS_PROMOTED
    score = store.probation_score("state_ca_board")
    assert score[0] == PROBATION_MIN_TRIALS  # every trial passed


def test_probation_agnes_fail_can_only_ever_fail_a_source(tmp_path):
    store = _store(tmp_path)
    _seed_with_adapter(store)
    dry_run(store, "state_ca_board", fetch_fn=_fetch(_xlsx_bytes(600)))
    out = probation_check_v2(store, "state_ca_board",
                             fetch_fn=_fetch(_xlsx_bytes(600)),
                             ai_ask=lambda p: "FAIL these are test rows")
    assert out["verdict"] is False
    assert out["detail"].startswith("agnes FAIL")
    assert store.probation_score("state_ca_board")[1] == 1


def test_probation_mechanical_failure_never_reaches_agnes(tmp_path):
    store = _store(tmp_path)
    _seed_with_adapter(store)
    dry_run(store, "state_ca_board", fetch_fn=_fetch(_xlsx_bytes(600)))
    called: list[str] = []
    out = probation_check_v2(
        store, "state_ca_board", fetch_fn=_fetch(_xlsx_bytes(10)),
        ai_ask=lambda p: (called.append(p), "PASS")[1])
    assert out["verdict"] is False
    assert called == []  # no opinion is asked about rows that failed gates
    assert "only 10 rows" in out["detail"]


def test_probation_dead_ai_is_no_opinion_and_still_records_the_trial(tmp_path):
    store = _store(tmp_path)
    _seed_with_adapter(store)
    dry_run(store, "state_ca_board", fetch_fn=_fetch(_xlsx_bytes(600)))

    def boom(prompt):
        raise RuntimeError("gateway 500")

    out = probation_check_v2(store, "state_ca_board",
                             fetch_fn=_fetch(_xlsx_bytes(600)), ai_ask=boom)
    assert out["verdict"] is True  # the mechanical gates decided
    assert out["agnes_reason"] == "LLM call failed: RuntimeError"
    assert store.probation_score("state_ca_board")[1] == 1


def test_probation_check_requires_probation_status(tmp_path):
    store = _store(tmp_path)
    _seed_with_adapter(store)
    with pytest.raises(ValueError, match="needs a source in probation"):
        probation_check_v2(store, "state_ca_board", fetch_fn=_fetch(b""))


def test_probation_pass_v2_walks_the_population_and_reports_honestly(tmp_path):
    store = _store(tmp_path)
    _seed_with_adapter(store)
    # a V1-era probation row (proposed → verified → probation) has no V2
    # adapter at all — the pass must skip it with the honest reason
    store.propose("legacy_soda_source", "soda_api", "Legacy SODA",
                  "https://data.ca.gov/resource/x.json")
    store.mark_verified("legacy_soda_source")
    store.start_probation("legacy_soda_source")
    dry_run(store, "state_ca_board", fetch_fn=_fetch(_xlsx_bytes(600)))

    out = probation_pass_v2(store, fetch_fn=_fetch(_xlsx_bytes(600)),
                            ai_ask=lambda p: "PASS")
    assert out["checked"] == ["state_ca_board"]
    assert out["skipped"] == {
        "legacy_soda_source": "no V2 adapter (fetch_spec empty)"}
    assert out["promoted"] == [] and out["retired"] == []


def test_judge_rows_unparseable_reply_is_an_absent_opinion():
    ok, why = judge_rows(lambda p: "I think they look fine to me",
                         field_map=_FIELD_MAP, columns=COLUMNS,
                         rows=[{"BusinessName": "Acme"}])
    assert ok is None and why == "unparseable reply"
    ok, why = judge_rows(lambda p: "PASS real businesses with dialable numbers",
                         field_map=_FIELD_MAP, columns=COLUMNS,
                         rows=[{"BusinessName": "Acme"}])
    assert ok is True and "real businesses" in why
