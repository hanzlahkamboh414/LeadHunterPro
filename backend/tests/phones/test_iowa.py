import io
import json
import zipfile

from app.discovery.sources.status import SourceStatus
from app.discovery.tradefold import normalize_trade
from app.phones.iowa import _parse_archive, fetch_ia_records
from app.phones.soda import fetchable_trade_coverage


def test_iowa_archive_and_trade_paging_exclude_expired_and_out_of_state():
    rows = [
        {"registration_number": "B", "business_name": "Second Electric",
         "phone": "5155550102", "city": "DES MOINES", "state": "IA",
         "expire_date": "2030-01-01", "primary_activity": "Electrical and Wiring Installation"},
        {"registration_number": "A", "business_name": "First Electric",
         "phone": "5155550101", "city": "AMES", "state": "IA",
         "expire_date": "2030-01-01", "primary_activity": "Electrical and Wiring Installation"},
        {"registration_number": "C", "business_name": "Expired Electric",
         "phone": "5155550103", "city": "AMES", "state": "IA",
         "expire_date": "2020-01-01", "primary_activity": "Electrical and Wiring Installation"},
        {"registration_number": "D", "business_name": "Nebraska Electric",
         "phone": "4025550104", "city": "OMAHA", "state": "NE",
         "expire_date": "2030-01-01", "primary_activity": "Electrical and Wiring Installation"},
    ]
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("contractors.json", "\n".join(json.dumps(row) for row in rows))
    parsed = _parse_archive(blob.getvalue(), min_rows=0)
    status, page, meta = fetch_ia_records("electrical", limit=1, offset=1,
                                          rows_fn=lambda: parsed)
    assert status == SourceStatus.SUCCESS
    assert [row["business_name"] for row in page] == ["Second Electric"]
    assert meta["rows_fetched"] == 1
    assert normalize_trade(page[0]["trade_category"]) == "electrical"


def test_iowa_only_advertises_unambiguous_trades():
    coverage = fetchable_trade_coverage()
    assert coverage["electrical"]["IA"] == "ia_registration"
    assert coverage["gc"]["IA"] == "ia_registration"
    assert "IA" not in coverage["plumbing"]
    assert "IA" not in coverage["mechanical"]
    assert normalize_trade("Residential Remodelers") == "gc"
