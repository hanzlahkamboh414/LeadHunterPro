"""Promoted board artifacts stock the phone pool once per content version."""

from app.phones.store import PhoneLeadsStore
from app.source_scout.bulk_sync import (
    sync_mn_state_only, sync_mn_verified_trades, sync_nyc_hic_state_only,
    sync_promoted,
)
from app.source_scout.store import ScoutStore


def _promoted(store, source_id="state_or_board", state="OR"):
    store.seed_upsert(
        source_id, seed_domain="phones", state=state,
        name="Official board", base_url="https://board.example.gov")
    store.start_probing(source_id)
    store.record_probe_success(source_id, "bulk_file")
    store.record_adapter(
        source_id,
        fetch_spec={
            "method": "GET",
            "url": f"https://{state.lower()}_board.gov/licenses.csv",
            "format": "csv", "file_shape": {"header_row": 1},
        },
        field_map={
            "company_name": "Business", "phone": "Phone",
            "trade": "Trade", "city": "City", "state_field": "MailState",
            "status": "Status",
        },
        trade_mapping={"General Contractor": "gc"},
        capabilities={"phone": {"present": True, "fill_rate": 1.0}},
    )
    store.mark_adapter_draft(source_id)
    store.enter_probation(source_id)
    store.promote(source_id)


def test_promoted_bulk_board_imports_once_and_uses_jurisdiction(tmp_path):
    store = ScoutStore(str(tmp_path / "scout.db"))
    phones = PhoneLeadsStore(str(tmp_path / "phones.db"))
    _promoted(store)
    artifact = (
        b"Business,Phone,Trade,City,MailState,Status\n"
        b"Acme,5039573450,General Contractor,Portland,WA,ACTIVE\n"
        b"Old Co,5039573451,General Contractor,Salem,OR,EXPIRED\n"
    )
    fetches = []

    def fetch(spec):
        fetches.append(spec["url"])
        return artifact

    first = sync_promoted(store, phones, fetch=fetch)
    assert first["inserted"] == 1
    assert phones.unclaimed_count("gc", "OR") == 1
    assert phones.unclaimed_count("gc", "WA") == 0
    assert store.get("state_or_board")["rows_consumed"] == 2

    second = sync_promoted(store, phones, fetch=fetch)
    assert second["unchanged"] == ["state_or_board"]
    assert second["inserted"] == 0
    assert store.get("state_or_board")["rows_consumed"] == 2
    assert len(fetches) == 2


def test_failed_board_does_not_stop_another_source(tmp_path):
    store = ScoutStore(str(tmp_path / "scout.db"))
    phones = PhoneLeadsStore(str(tmp_path / "phones.db"))
    _promoted(store, "state_or_board", "OR")
    _promoted(store, "state_wa_board", "WA")

    def fetch(spec):
        if "or_board" in spec["url"]:
            raise OSError("board offline")
        return b"Business,Phone,Trade,City,MailState,Status\nAcme,5039573450,General Contractor,Seattle,WA,ACTIVE\n"

    out = sync_promoted(store, phones, fetch=fetch)
    assert "state_or_board" in out["errors"]
    assert out["inserted"] == 1


def test_mn_state_only_import_is_filtered_and_idempotent(tmp_path):
    store = ScoutStore(str(tmp_path / "scout.db"))
    phones = PhoneLeadsStore(str(tmp_path / "phones.db"))
    lines = ["Name,Phone_No,Status,City,St,License_Type"]
    lines.extend(
        f"North Star Construction {n},612555{n:04d},Issued,Minneapolis,WI,Contractor Registration"
        for n in range(50)
    )
    lines[1] = lines[1].replace("North Star", "Nordic\u00a0Star")
    lines.extend([
        "Old Co,6125559000,Expired,St Paul,MN,Contractor Registration",
        "Bad Phone,123,Issued,St Paul,MN,Contractor Registration",
        "Wrong Type,6125559001,Issued,St Paul,MN,Other License",
        ",6125559002,Issued,St Paul,MN,Contractor Registration",
    ])
    artifact = ("\n".join(lines) + "\n").encode("cp1252")
    first = sync_mn_state_only(store, phones, fetch=lambda spec: artifact)
    assert first["inserted"] == 50
    assert phones.pool_stats()["by_state"]["MN"] == 50
    assert phones.unclaimed_count("", "MN") == 0  # raw, not qualified
    assert len(phones.pending_trade_enrichment(100)) == 50
    assert phones.unclaimed_count("gc", "MN") == 0
    assert phones.unclaimed_count("", "WI") == 0
    second = sync_mn_state_only(store, phones, fetch=lambda spec: artifact)
    assert second["unchanged"] == ["mn_dli_registration"]
    assert second["inserted"] == 0


def test_mn_schema_failure_does_not_advance_snapshot(tmp_path):
    store = ScoutStore(str(tmp_path / "scout.db"))
    phones = PhoneLeadsStore(str(tmp_path / "phones.db"))
    out = sync_mn_state_only(
        store, phones, fetch=lambda spec: b"Name,Status\nAcme,Issued\n")
    assert "mn_dli_registration" in out["errors"]
    assert store.snapshot_checksum("mn_dli_registration") == ""
    assert phones.unclaimed_count("", "MN") == 0


def test_mn_official_license_join_promotes_only_exact_unambiguous_match(tmp_path):
    import io
    import zipfile

    store = ScoutStore(str(tmp_path / "scout.db"))
    phones = PhoneLeadsStore(str(tmp_path / "phones.db"))
    for name, phone in (("North Star Roofing", "6125550100"),
                        ("Ambiguous Co", "6125550101"),
                        ("Wrong Zip Co", "6125550102"),
                        ("Person License Co", "6125550103")):
        phones.add([{
            "phone": phone, "business_name": name, "trade_category": "",
            "state_only": True, "city": "Minneapolis", "state": "MN",
            "source": "mn_dli_registration",
        }])
    reg = ["Name,Phone_No,Zip,Status,License_Type",
           "North Star Roofing,6125550100,55401,Issued,Contractor Registration",
           "Ambiguous Co,6125550101,55402,Issued,Contractor Registration",
           "Wrong Zip Co,6125550102,55404,Issued,Contractor Registration",
           "Person License Co,6125550103,55405,Issued,Contractor Registration"]
    reg.extend(
        f"Other {n},612555{n + 1000:04d},55403,Issued,Contractor Registration"
        for n in range(46))
    licensed = ["Name,Phone_No,Zip,Status,Bus_Pers,License_Type,License_Subtype,Lic_Number",
                "North Star Roofing,6125550100,55401,Issued,Business,Residential Contractors,Residential Roofer Contractor,BC123456",
                "Ambiguous Co,6125550101,55402,Issued,Business,Residential Contractors,Residential Roofer Contractor,BC123457",
                "Ambiguous Co,6125550101,55402,Issued,Business,Plumbing,Plumbing Contractor,PC123457",
                "Wrong Zip Co,6125550102,55499,Issued,Business,Residential Contractors,Residential Roofer Contractor,BC123458",
                "Person License Co,6125550103,55405,Issued,Person,Residential Contractors,Residential Roofer Contractor,BC123459"]
    licensed.extend(
        f"Other {n},612555{n + 2000:04d},55403,Expired,Business,Electrical,Class A Electrical Contractor,EA{n:06d}"
        for n in range(45))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("LIC_SNAP_09_24_2026.csv", "\n".join(licensed).encode("cp1252"))

    def fetch(spec):
        return buffer.getvalue() if spec["url"].endswith(".zip") else \
            "\n".join(reg).encode("cp1252")

    result = sync_mn_verified_trades(store, phones, fetch=fetch)
    assert result["verified"] == 1
    assert result["ambiguous"] == 1
    assert phones.unclaimed_count("roofing", "MN") == 1
    served = phones.serve("roofing", "MN", "", 1, "alice")
    assert served[0]["business_name"] == "North Star Roofing"
    assert served[0]["trade_evidence_kind"] == "official_license"
    assert served[0]["trade_evidence_ref"] == "BC123456"
    assert phones.unqualified_count("MN") == 3
    assert sync_mn_verified_trades(store, phones, fetch=fetch)["verified"] == 0


def test_nyc_hic_state_only_import_filters_and_dedups(tmp_path):
    store = ScoutStore(str(tmp_path / "scout.db"))
    phones = PhoneLeadsStore(str(tmp_path / "phones.db"))
    lines = [
        "business_name,contact_phone,business_category,license_status,address_city,address_state"
    ]
    lines.extend(
        f"New York General Contracting {n},718555{n:04d},Home Improvement Contractor,Active,BRONX,NJ"
        for n in range(50)
    )
    lines.extend([
        "Other Co,7185559000,Other Category,Active,BRONX,NY",
        "Expired Co,7185559001,Home Improvement Contractor,Expired,BRONX,NY",
        "Bad Phone,123,Home Improvement Contractor,Active,BRONX,NY",
    ])
    artifact = ("\n".join(lines) + "\n").encode()
    first = sync_nyc_hic_state_only(store, phones,
                                    fetch=lambda spec: artifact)
    assert first["inserted"] == 50
    assert phones.pool_stats()["by_state"]["NY"] == 50
    assert phones.unclaimed_count("", "NY") == 0  # raw, not qualified
    assert len(phones.pending_trade_enrichment(100)) == 50
    assert phones.unclaimed_count("gc", "NY") == 0
    assert phones.unclaimed_count("", "NJ") == 0
    second = sync_nyc_hic_state_only(store, phones,
                                     fetch=lambda spec: artifact)
    assert second["unchanged"] == ["nyc_dcwp_hic"]
    assert second["inserted"] == 0


def test_nyc_hic_schema_failure_does_not_advance_snapshot(tmp_path):
    store = ScoutStore(str(tmp_path / "scout.db"))
    phones = PhoneLeadsStore(str(tmp_path / "phones.db"))
    out = sync_nyc_hic_state_only(
        store, phones, fetch=lambda spec: b"business_name,license_status\nAcme,Active\n")
    assert "nyc_dcwp_hic" in out["errors"]
    assert store.snapshot_checksum("nyc_dcwp_hic") == ""


def test_nyc_hic_capped_export_is_not_marked_complete(tmp_path):
    store = ScoutStore(str(tmp_path / "scout.db"))
    phones = PhoneLeadsStore(str(tmp_path / "phones.db"))
    row = {
        "business_name": "Acme", "contact_phone": "7185550100",
        "business_category": "Home Improvement Contractor",
        "license_status": "Active", "address_city": "BRONX",
        "address_state": "NY",
    }
    out = sync_nyc_hic_state_only(
        store, phones, fetch=lambda spec: b"new artifact",
        parse=lambda *_args, **_kw: (list(row), [row] * 50000),
    )
    assert "row cap" in out["errors"]["nyc_dcwp_hic"]
    assert store.snapshot_checksum("nyc_dcwp_hic") == ""
    assert phones.unclaimed_count("", "NY") == 0
