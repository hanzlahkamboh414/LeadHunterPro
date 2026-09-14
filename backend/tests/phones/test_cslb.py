"""P8 part 2 — CSLB bulk-sync connector contracts (fully hermetic).

The portal itself is UNREACHABLE for scripts (F5 transport fingerprint —
see app/phones/cslb.py), which is exactly why these tests exist: the
contract that matters is the PARSER (a downloaded xlsx -> phone-lead
records), the CLASSIFICATION MAP (code/label -> slug, with the
normalize_trade round-trip guarantee), and the FETCH LANE'S HONEST
REFUSAL (cslb_portal never pretends to be a fetchable source). None of
that needs the network — a real xlsx is built in-memory here with the
same stdlib the parser reads (zipfile + ElementTree, the two entries
the parser touches: xl/sharedStrings.xml + xl/worksheets/sheet.xml).
"""

from __future__ import annotations

import io
import re
import zipfile

from app.discovery.sources.status import SourceStatus
from app.discovery.tradefold import CANONICAL_TRADES, normalize_trade
from app.phones.cslb import (
    CSLB_CLASSIFICATIONS,
    CSLB_CODE_MAP,
    PORTAL_URL,
    SOURCE_ID,
    parse_xlsx,
)
from app.phones.soda import (
    TRADE_COVERAGE,
    effective_trade_coverage,
    fetch_license_records,
    fetchable_trade_coverage,
)
from app.phones.store import PhoneLeadsStore

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

#: The portal's verified 14-column header (live capture, 2026-09-14).
HEADER = [
    "LicenseNumber", "LastUpdated", "BusinessType", "BusinessName",
    "Address", "City", "State", "Zip", "County", "PhoneNumber",
    "IssueDate", "ExpirationDate", "Classification", "Status",
]


def _build_xlsx(rows: list[list[str]]) -> bytes:
    """A minimal but real xlsx with the entries the parser reads."""
    strings: list[str] = []
    index: dict[str, int] = {}

    def _shared(v: str) -> str:
        if v not in index:
            index[v] = len(strings)
            strings.append(v)
        return str(index[v])

    body: list[str] = []
    for row in rows:
        cells = "".join(
            f'<c t="s"><v>{_shared(v)}</v></c>' if v else "<c/>"
            for v in row
        )
        body.append(f"<row>{cells}</row>")
    shared_items = "".join(f"<si><t>{v}</t></si>" for v in strings)
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{_NS}" count="{len(strings)}" '
        f'uniqueCount="{len(strings)}">{shared_items}</sst>'
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_NS}"><sheetData>'
        f"{''.join(body)}</sheetData></worksheet>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", shared_xml)
        z.writestr("xl/worksheets/sheet.xml", sheet_xml)
    return buf.getvalue()


def _row(business: str, city: str, phone: str, state: str = "CA",
         status: str = "CLEAR") -> list[str]:
    return [
        "1094728", "09/14/2026", " Sole Proprietor", business,
        "123 MAIN ST", city, state, "90001", "Los Angeles", phone,
        "01/02/2020", "01/02/2028", "B-2", status,
    ]


# ---------------------------------------------------------------------------
# classification map — codes, labels, and the round-trip guarantee
# ---------------------------------------------------------------------------

def test_classifications_shape():
    for slug, pairs in CSLB_CLASSIFICATIONS.items():
        assert slug in CANONICAL_TRADES, f"{slug} is not a canonical trade"
        assert pairs, f"{slug} has no codes"
        for code, label in pairs:
            assert re.fullmatch(r"[A-Z0-9-]+", code), f"bad code {code!r}"
            assert label.strip() == label and label, f"bad label {label!r}"


def test_code_map_is_the_inverse():
    assert CSLB_CODE_MAP == {
        code: (slug, label)
        for slug, pairs in CSLB_CLASSIFICATIONS.items()
        for code, label in pairs
    }
    # codes are unique — one code can never mean two slugs
    assert len(CSLB_CODE_MAP) == sum(
        len(pairs) for pairs in CSLB_CLASSIFICATIONS.values()
    )


def test_labels_round_trip_to_their_slug():
    """THE ingest guarantee: trade_category is the classification LABEL, and
    PhoneLeadsStore.add folds it via normalize_trade — so the label must fold
    back to exactly the slug that requested the sync. If a future label edit
    breaks this, the rows would stock under a wrong/no trade."""
    for slug, pairs in CSLB_CLASSIFICATIONS.items():
        for _code, label in pairs:
            assert normalize_trade(label) == slug, (
                f"label {label!r} does not fold to {slug!r}"
            )


def test_lumber_and_mep_are_honestly_absent():
    """No CSLB code honestly maps to these trades — absent, never guessed."""
    assert "lumber" not in CSLB_CLASSIFICATIONS
    assert "mep" not in CSLB_CLASSIFICATIONS


def test_trade_coverage_ca_entries():
    for slug in CSLB_CLASSIFICATIONS:
        assert TRADE_COVERAGE[slug]["CA"] == SOURCE_ID
    assert "CA" not in TRADE_COVERAGE.get("lumber", {})
    assert "CA" not in TRADE_COVERAGE.get("mep", {})


# ---------------------------------------------------------------------------
# xlsx parsing (built in-memory, the real stdlib shape)
# ---------------------------------------------------------------------------

def test_parse_xlsx_happy_path():
    data = _build_xlsx([
        HEADER,
        _row("SMITH REMODELING", "LOS ANGELES", "8185551234"),
        _row("OCEAN VIEW KITCHENS LLC", "SAN DIEGO", "6195559876"),
    ])
    records = parse_xlsx(data, label="Residential Remodeling Contractor")
    assert len(records) == 2
    rec = records[0]
    assert rec["phone"] == "8185551234"
    assert rec["person_name"] == ""  # CSLB lists businesses only — honest
    assert rec["business_name"] == "SMITH REMODELING"
    assert rec["trade_category"] == "Residential Remodeling Contractor"
    assert rec["city"] == "LOS ANGELES"
    assert rec["state"] == "CA"
    assert rec["source"] == SOURCE_ID
    assert rec["license_status"] == "CLEAR"
    assert rec["source_url"] == PORTAL_URL
    assert records[1]["business_name"] == "OCEAN VIEW KITCHENS LLC"


def test_parse_xlsx_state_cell_wins_empty_defaults_ca():
    data = _build_xlsx([
        HEADER,
        _row("A", "FRESNO", "5595550000", state="NV"),  # out-of-state row
        _row("B", "FRESNO", "5595550001", state=""),    # blank State cell
    ])
    records = parse_xlsx(data, label="Plumbing Contractor")
    assert records[0]["state"] == "NV"
    assert records[1]["state"] == "CA"


def test_parse_xlsx_invalid_input_returns_empty():
    assert parse_xlsx(b"not a zip at all", label="Roofing Contractor") == []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet.xml", "not xml <<<")
    assert parse_xlsx(buf.getvalue(), label="Roofing Contractor") == []


# ---------------------------------------------------------------------------
# the fetch lane's honest refusal + coverage split
# ---------------------------------------------------------------------------

def test_fetch_lane_never_serves_cslb():
    status, records, meta = fetch_license_records(SOURCE_ID, "gc")
    assert status == SourceStatus.ERROR
    assert records == []
    assert "sync_cslb" in meta["error"]
    assert meta["source"] == SOURCE_ID


def test_browser_synced_pairs_excluded_from_fetchable():
    """Coverage metadata stays truthful (a CA search reports what covers it)
    while the harvester's fetch lane can never select a browser-synced pair."""
    assert effective_trade_coverage()["plumbing"]["CA"] == SOURCE_ID
    assert "CA" not in fetchable_trade_coverage()["plumbing"]
    assert "CA" not in fetchable_trade_coverage()["gc"]
    # The fetchable map is not emptied — WA/TDLR pairs survive the filter.
    assert fetchable_trade_coverage()["gc"]["WA"] == "wa_license"


# ---------------------------------------------------------------------------
# ingest — the store folds the label to the canonical slug
# ---------------------------------------------------------------------------

def test_store_add_folds_cslb_label_to_slug(tmp_path):
    store = PhoneLeadsStore(db_path=str(tmp_path / "phones.db"))
    data = _build_xlsx([
        HEADER,
        _row("GOLDEN STATE PIPING", "SACRAMENTO", "9165551122"),
        _row("BAY DRYWALL INC", "OAKLAND", "5105553344"),
    ])
    records = parse_xlsx(data, label="Plumbing Contractor")
    # Both rows carry the same synced label (the file IS one classification).
    records[1]["trade_category"] = "Drywall Contractor"
    counts = store.add(records)
    assert counts == {"inserted": 2, "duplicate": 0,
                      "dropped_bad_phone": 0, "suppressed": 0}
    leads = store.serve(trade="plumbing", state="CA", city="", limit=10,
                        user_id="u1")
    assert [l["business_name"] for l in leads] == ["GOLDEN STATE PIPING"]
    drywall = store.serve(trade="drywall", state="CA", city="", limit=10,
                          user_id="u1")
    assert [l["business_name"] for l in drywall] == ["BAY DRYWALL INC"]
    # Idempotent re-sync: the store's dedupe makes a re-run a no-op.
    assert store.add(records)["duplicate"] == 2
