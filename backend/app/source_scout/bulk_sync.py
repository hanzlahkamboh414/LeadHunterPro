"""Import promoted non-SODA board adapters into the shared phone pool."""

from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from collections import defaultdict
from typing import Any, Callable
from urllib.parse import urlencode

from app.discovery.tradefold import normalize_trade
from app.phones.store import PhoneLeadsStore, normalize_phone
from app.source_scout.adapter_writer import adapter_of
from app.source_scout.boards import load_state_boards
from app.source_scout.bulk_fetch import fetch_bytes
from app.source_scout.store import ScoutStore
from app.source_scout.tabular import read_rows

logger = logging.getLogger(__name__)

_BATCH = 1000
_BAD_STATUSES = ("expired", "revoked", "suspended", "inactive", "cancelled")
_MN_SOURCE_ID = "mn_dli_registration"
_MN_MASTER_URL = (
    "https://secure.doli.state.mn.us/ccld/data/MNDLILicRegCertExport.zip"
)
_MN_LICENSE_TRADES = {
    "Residential Building Contractor": "gc",
    "Residential Remodeler Contractor": "gc",
    "Residential Roofer Contractor": "roofing",
    "Class A Electrical Contractor": "electrical",
    "Plumbing Contractor": "plumbing",
    "Restricted Plumbing Contractor": "plumbing",
}
_MN_COLUMNS = frozenset({
    "Name", "Phone_No", "Status", "City", "St", "License_Type",
})
_NYC_SOURCE_ID = "nyc_dcwp_hic"
_NYC_LIMIT = 50000
_NYC_COLUMNS = frozenset({
    "business_name", "contact_phone", "business_category",
    "license_status", "address_city", "address_state",
})
_NYC_DATASET = "https://data.cityofnewyork.us/resource/w7w3-xahh.csv"


def _record(raw: dict[str, Any], fields: dict[str, str],
            trades: dict[str, str], *, source_id: str, state: str,
            source_url: str) -> dict[str, Any] | None:
    source_trade = str(raw.get(fields.get("trade", ""), "") or "").strip()
    slug = trades.get(source_trade) or normalize_trade(source_trade)
    if not slug:
        return None
    status = str(raw.get(fields.get("status", ""), "") or "").strip()
    if any(bad in status.lower() for bad in _BAD_STATUSES):
        return None
    return {
        "phone": raw.get(fields.get("phone", ""), ""),
        "person_name": "",
        "business_name": raw.get(fields.get("company_name", ""), ""),
        "trade_category": slug,
        "city": raw.get(fields.get("city", ""), ""),
        # This is the licence jurisdiction, never a mailing-address state.
        "state": state,
        "source": source_id,
        "license_status": status,
        "source_url": source_url,
    }


def sync_promoted(
    store: ScoutStore,
    phone_store: PhoneLeadsStore,
    *,
    fetch: Callable[..., bytes] = fetch_bytes,
    parse: Callable[..., tuple[list[str], list[dict[str, str]]]] = read_rows,
) -> dict[str, Any]:
    """Fetch each promoted phone-capable bulk source once per daily pass.

    Source failures are reported per source so one broken board cannot
    prevent the others from syncing. A source with no V2 fetch contract is
    served by the existing SODA lane and is intentionally skipped here.
    """
    summary: dict[str, Any] = {
        "checked": [], "unchanged": [], "inserted": 0, "errors": {},
    }
    for candidate in store.promoted_for_vertical("phone"):
        source_id = candidate["source_id"]
        row = store.get(source_id)
        if row is None:
            continue
        adapter = adapter_of(row)
        spec = adapter["fetch_spec"]
        fields = adapter["field_map"]
        state = str(row.get("state", "")).strip().upper()
        if not spec:
            continue  # V1 SODA source; its cursor lane owns the import
        if not state or not fields.get("phone") or not fields.get("trade"):
            summary["errors"][source_id] = (
                "promoted adapter lacks state, phone or trade mapping")
            continue
        try:
            data = fetch(spec)
            checksum = hashlib.sha256(data).hexdigest()
            summary["checked"].append(source_id)
            if store.snapshot_checksum(source_id) == checksum:
                summary["unchanged"].append(source_id)
                continue
            _, rows = parse(
                data, fmt=str(spec.get("format", "")),
                file_shape=spec.get("file_shape") or {}, max_rows=0,
            )
            inserted = 0
            for start in range(0, len(rows), _BATCH):
                records = [
                    rec for raw in rows[start:start + _BATCH]
                    if (rec := _record(
                        raw, fields, adapter["trade_mapping"],
                        source_id=source_id, state=state,
                        source_url=str(spec.get("url", "")),
                    )) is not None
                ]
                inserted += phone_store.add(records)["inserted"]
            store.record_snapshot(
                source_id, checksum, rows_seen=len(rows), inserted=inserted)
            summary["inserted"] += inserted
            logger.info(
                "source sync %s: %d rows, %d new phone leads",
                source_id, len(rows), inserted,
            )
        except Exception as exc:  # noqa: BLE001 — isolate one failing board
            summary["errors"][source_id] = f"{type(exc).__name__}: {exc}"[:200]
            logger.exception("source sync %s failed", source_id)
    return summary


def sync_mn_state_only(
    store: ScoutStore,
    phone_store: PhoneLeadsStore,
    *,
    fetch: Callable[..., bytes] = fetch_bytes,
    parse: Callable[..., tuple[list[str], list[dict[str, str]]]] = read_rows,
) -> dict[str, Any]:
    """Stock the verified MN registration export without inventing a trade.

    The seed URL is the single maintained endpoint. ``St`` is a mailing
    address, not the licence jurisdiction; every accepted row belongs to MN.
    """
    summary: dict[str, Any] = {
        "checked": [], "unchanged": [], "inserted": 0, "errors": {},
    }
    try:
        seed = next(r for r in load_state_boards() if r["state"] == "MN")
        spec = {"method": "GET", "url": seed["base_url"], "format": "csv"}
        data = fetch(spec)
        checksum = hashlib.sha256(data).hexdigest()
        summary["checked"].append(_MN_SOURCE_ID)
        if store.snapshot_checksum(_MN_SOURCE_ID) == checksum:
            summary["unchanged"].append(_MN_SOURCE_ID)
            return summary
        columns, rows = parse(data, fmt="csv", file_shape={
            "header_row": 1, "encoding": "cp1252"},
                              max_rows=0)
        if not _MN_COLUMNS.issubset(columns) or len(rows) < 50:
            raise ValueError("MN registration export schema/volume gate failed")
        inserted = 0
        for start in range(0, len(rows), _BATCH):
            records = []
            for raw in rows[start:start + _BATCH]:
                if raw["Status"].strip().lower() != "issued":
                    continue
                if raw["License_Type"].strip() != "Contractor Registration":
                    continue
                if not raw["Name"].strip() or not normalize_phone(raw["Phone_No"]):
                    continue
                records.append({
                    "phone": raw["Phone_No"],
                    "business_name": raw["Name"],
                    "trade_category": "",
                    "state_only": True,
                    "city": raw["City"],
                    "state": "MN",
                    "source": _MN_SOURCE_ID,
                    "license_status": raw["Status"],
                    "source_url": spec["url"],
                })
            inserted += phone_store.add(records)["inserted"]
        store.record_snapshot(
            _MN_SOURCE_ID, checksum, rows_seen=len(rows), inserted=inserted)
        summary["inserted"] = inserted
        logger.info("MN registration sync: %d rows, %d new phone leads",
                    len(rows), inserted)
    except Exception as exc:  # noqa: BLE001 — one board cannot stop discovery
        summary["errors"][_MN_SOURCE_ID] = f"{type(exc).__name__}: {exc}"[:200]
        logger.exception("MN registration sync failed")
    return summary


def _mn_identity(raw: dict[str, str]) -> tuple[str, str, str]:
    """Exact agency name + phone + five-digit ZIP, never a fuzzy guess."""
    name = " ".join(raw.get("Name", "").split()).casefold()
    phone = normalize_phone(raw.get("Phone_No", ""))
    zip_code = "".join(c for c in raw.get("Zip", "") if c.isdigit())[:5]
    return name, phone, zip_code


def sync_mn_verified_trades(
    store: ScoutStore,
    phone_store: PhoneLeadsStore,
    *,
    fetch: Callable[..., bytes] = fetch_bytes,
    parse: Callable[..., tuple[list[str], list[dict[str, str]]]] = read_rows,
) -> dict[str, Any]:
    """Join MN registrations to active official business licenses.

    The master file contains personal licenses too; only exact business
    identities with one unambiguous contractor subtype can promote a raw
    registration. The official license number is retained as a proof ref.
    """
    summary: dict[str, Any] = {
        "checked": [], "verified": 0, "ambiguous": 0, "errors": {},
    }
    try:
        seed = next(r for r in load_state_boards() if r["state"] == "MN")
        registration = fetch({"method": "GET", "url": seed["base_url"],
                              "format": "csv"})
        reg_columns, reg_rows = parse(
            registration, fmt="csv", file_shape={"header_row": 1,
                                                  "encoding": "cp1252"},
            max_rows=0)
        if not {"Name", "Phone_No", "Zip", "Status", "License_Type"}.issubset(
                reg_columns) or len(reg_rows) < 50:
            raise ValueError("MN registration join schema/volume gate failed")

        master = fetch({"method": "GET", "url": _MN_MASTER_URL,
                        "format": "zip/csv"})
        with zipfile.ZipFile(io.BytesIO(master)) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError("MN master zip must contain exactly one CSV")
        master_columns, master_rows = parse(
            master, fmt="zip/csv", file_shape={
                "inner_path": members[0], "header_row": 1,
                "encoding": "cp1252"}, max_rows=0)
        required = {"Name", "Phone_No", "Zip", "Status", "Bus_Pers",
                    "License_Subtype", "Lic_Number"}
        if not required.issubset(master_columns) or len(master_rows) < 50:
            raise ValueError("MN master license schema/volume gate failed")

        licensed: dict[tuple[str, str, str], set[tuple[str, str]]] = defaultdict(set)
        for row in master_rows:
            trade = _MN_LICENSE_TRADES.get(row["License_Subtype"].strip(), "")
            key = _mn_identity(row)
            if (row["Status"].strip().lower() == "issued" and
                    row["Bus_Pers"].strip().lower() == "business" and
                    trade and row["Lic_Number"].strip() and all(key)):
                licensed[key].add((trade, row["Lic_Number"].strip()))

        summary["checked"] = [_MN_SOURCE_ID]
        for row in reg_rows:
            key = _mn_identity(row)
            if (row["Status"].strip().lower() != "issued" or
                    row["License_Type"].strip() != "Contractor Registration" or
                    not all(key)):
                continue
            matches = licensed.get(key, set())
            trades = {trade for trade, _ in matches}
            if len(trades) > 1:
                summary["ambiguous"] += 1
                continue
            if not trades:
                continue
            lead_id = phone_store.raw_lead_id(
                source=_MN_SOURCE_ID, phone=row["Phone_No"],
                business_name=row["Name"])
            if lead_id is None:
                continue
            trade = next(iter(trades))
            license_number = sorted(ref for t, ref in matches if t == trade)[0]
            if phone_store.set_trade_resolution(
                    lead_id, trade=trade, evidence_url=_MN_MASTER_URL,
                    evidence_kind="official_license",
                    evidence_ref=license_number):
                summary["verified"] += 1
        store.record_snapshot(
            "mn_dli_master_join", hashlib.sha256(registration + master).hexdigest(),
            rows_seen=len(master_rows), inserted=summary["verified"])
        logger.info("MN official license join: %d verified, %d ambiguous",
                    summary["verified"], summary["ambiguous"])
    except Exception as exc:  # noqa: BLE001 — one board cannot stop discovery
        summary["errors"]["mn_dli_master_join"] = \
            f"{type(exc).__name__}: {exc}"[:200]
        logger.exception("MN official license join failed")
    return summary


def sync_nyc_hic_state_only(
    store: ScoutStore,
    phone_store: PhoneLeadsStore,
    *,
    fetch: Callable[..., bytes] = fetch_bytes,
    parse: Callable[..., tuple[list[str], list[dict[str, str]]]] = read_rows,
) -> dict[str, Any]:
    """Stock active NYC home-improvement licensees without guessing trade.

    DCWP licenses this category in NYC, not across New York State. A NY
    state-only search may serve these rows, with their actual borough city.
    """
    summary: dict[str, Any] = {
        "checked": [], "unchanged": [], "inserted": 0, "errors": {},
    }
    try:
        query = urlencode({
            "$where": "business_category='Home Improvement Contractor' "
                      "AND license_status='Active' AND contact_phone IS NOT NULL",
            "$order": ":id", "$limit": str(_NYC_LIMIT),
        })
        spec = {"method": "GET", "url": f"{_NYC_DATASET}?{query}",
                "format": "csv"}
        data = fetch(spec)
        checksum = hashlib.sha256(data).hexdigest()
        summary["checked"].append(_NYC_SOURCE_ID)
        if store.snapshot_checksum(_NYC_SOURCE_ID) == checksum:
            summary["unchanged"].append(_NYC_SOURCE_ID)
            return summary
        columns, rows = parse(data, fmt="csv", file_shape={
            "header_row": 1, "encoding": "utf-8"}, max_rows=0)
        if not _NYC_COLUMNS.issubset(columns) or len(rows) < 50:
            raise ValueError("NYC HIC export schema/volume gate failed")
        if len(rows) >= _NYC_LIMIT:
            raise ValueError("NYC HIC export reached row cap; pagination required")
        inserted = 0
        for start in range(0, len(rows), _BATCH):
            records = []
            for raw in rows[start:start + _BATCH]:
                if raw["business_category"] != "Home Improvement Contractor":
                    continue
                if raw["license_status"].strip().lower() != "active":
                    continue
                if not raw["business_name"].strip() or not normalize_phone(
                        raw["contact_phone"]):
                    continue
                records.append({
                    "phone": raw["contact_phone"],
                    "business_name": raw["business_name"],
                    "trade_category": "",
                    "state_only": True,
                    "city": raw["address_city"],
                    # NY is the licence jurisdiction; address_state is a
                    # mailing location and never used for routing.
                    "state": "NY",
                    "source": _NYC_SOURCE_ID,
                    "license_status": raw["license_status"],
                    "source_url": _NYC_DATASET,
                })
            inserted += phone_store.add(records)["inserted"]
        store.record_snapshot(
            _NYC_SOURCE_ID, checksum, rows_seen=len(rows), inserted=inserted)
        summary["inserted"] = inserted
        logger.info("NYC HIC sync: %d rows, %d new phone leads",
                    len(rows), inserted)
    except Exception as exc:  # noqa: BLE001 — one board cannot stop discovery
        summary["errors"][_NYC_SOURCE_ID] = f"{type(exc).__name__}: {exc}"[:200]
        logger.exception("NYC HIC sync failed")
    return summary
