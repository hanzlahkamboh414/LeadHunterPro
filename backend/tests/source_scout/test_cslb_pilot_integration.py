"""The REAL CSLB export through the whole V2 chain — opt-in integration.

``output/cslb_pilot_B-2.xlsx`` is a live CSLB download (B-2, 1,596 rows,
99.6% phone fill) fetched by ``scripts/pilot_ca_cslb.py``. When that
artifact is present this test pushes it through every phase-3 stage with
the AI stubbed by a contract that mirrors what the Adapter Writer asks
for — proving the contract gate, the Validator's measurements and the
probation thresholds hold on real source bytes rather than on rows this
test invented.

The artifact is gitignored, so the test skips when it is absent:
    python scripts/pilot_ca_cslb.py --trials 1
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.phones.cslb import CSLB_CODE_MAP, PORTAL_URL
from app.source_scout.adapter_writer import validate_contract
from app.source_scout.dryrun import dry_run, probation_check_v2
from app.source_scout.probation import PROBATION_MIN_TRIALS
from app.source_scout.store import STATUS_PROMOTED, ScoutStore
from app.source_scout.tabular import read_rows

ARTIFACT = (Path(__file__).resolve().parents[2] / "output"
            / "cslb_pilot_B-2.xlsx")
SOURCE_ID = "state_ca_board"
KNOWN_TRADES = {code: slug for code, (slug, _l) in CSLB_CODE_MAP.items()}

pytestmark = pytest.mark.skipif(
    not ARTIFACT.exists(),
    reason="live CSLB artifact absent (output/ is gitignored)")

#: What the Adapter Writer must produce for this source — the same facts
#: the real page and the real table carry (see the pilot's evidence file).
REPLY = {
    "fetch": {
        "method": "POST", "url": PORTAL_URL, "format": "xlsx",
        "refresh": "monthly", "file_shape": {"header_row": 1},
        "form": {"select_field": "ctl00$MainContent$lbClassification",
                 "code": "B-2",
                 "submit_field": "ctl00$MainContent$btnSearch",
                 "submit_value": "Download"},
    },
    "field_map": {"company_name": "BusinessName", "phone": "PhoneNumber",
                  "address": "Address", "city": "City", "state_field": "State",
                  "license_no": "LicenseNumber", "trade": "Classification",
                  "status": "Status"},
    "capabilities": {"phone": {"present": True}, "address": {"present": True},
                     "city": {"present": True},
                     "license_no": {"present": True},
                     "trade": {"present": True}, "status": {"present": True},
                     "email": {"present": False}},
    "trade_mapping": KNOWN_TRADES,
    "estimated_rows": 1596,
}


def _artifact_bytes() -> bytes:
    return ARTIFACT.read_bytes()


def _fetch(spec, transport=None):  # noqa: ARG001 — the stored spec is asserted
    return _artifact_bytes()


def test_real_cslb_export_promotes_through_the_whole_v2_chain(tmp_path):
    data = _artifact_bytes()
    columns, rows = read_rows(data, fmt="xlsx", file_shape={"header_row": 1})
    assert len(rows) == 1596 and "PhoneNumber" in columns

    # the contract gate, on the real columns
    contract = validate_contract(REPLY, columns=columns, access_path="html_form",
                                 source_url="https://www.cslb.ca.gov")
    assert contract["field_map"]["phone"] == "PhoneNumber"

    store = ScoutStore(db_path=str(tmp_path / "scout.db"))
    store.seed_upsert(SOURCE_ID, seed_domain="phones", state="CA", name="CSLB",
                      base_url="https://www.cslb.ca.gov",
                      seed_meta={"priority_rank": 1, "known_access": "bulk_file"})
    store.start_probing(SOURCE_ID)
    store.record_probe_success(SOURCE_ID, "html_form")
    store.record_adapter(SOURCE_ID, fetch_spec=contract["fetch"],
                         field_map=contract["field_map"],
                         trade_mapping=contract["trade_mapping"],
                         capabilities=contract["capabilities"])
    store.mark_adapter_draft(SOURCE_ID)

    # the 500-row dry run measures the real file
    run = dry_run(store, SOURCE_ID, fetch_fn=_fetch)
    result = run["result"]
    assert result.passed, result.summary_reason
    assert len(run["rows"]) == 500            # the §6 dry-run size
    assert result.estimated_rows == 1596      # re-measured, not the estimate
    assert result.capabilities["phone"]["present"] is True
    assert result.capabilities["phone"]["fill_rate"] >= 0.95
    assert result.capabilities["email"]["present"] is False
    assert run["row"]["status"] == "probation"

    # probation trials on the same real bytes (the live spacing is the
    # pilot script's job; here the gates and thresholds are what matter)
    statuses = [probation_check_v2(store, SOURCE_ID, fetch_fn=_fetch)["status"]
                for _ in range(PROBATION_MIN_TRIALS)]
    assert statuses[-1] == STATUS_PROMOTED

    promoted = store.promoted_for_vertical("phone")
    assert [p["source_id"] for p in promoted] == [SOURCE_ID]
    caps = promoted[0]["capabilities"]
    caps = json.loads(caps) if isinstance(caps, str) else caps
    assert caps["phone"]["present"] is True
    assert promoted[0]["estimated_rows"] == 1596
