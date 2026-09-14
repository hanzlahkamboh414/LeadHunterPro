"""CSLB (California Contractors State License Board) — bulk-sync connector
(P8 part 2).

The ListByClassification data portal is behind an F5 edge that REJECTS
every scripted transport (curl/httpx replay of a working browser request
fails with ``Request Rejected`` — the edge fingerprints TLS/JA3 and HTTP
frames, not just cookies), and rapid automated sessions get rate-blocked
(504 + rejection after three sessions in ten minutes, verified
2026-09-14). The only automatable path is a HEADED real browser
(patchright + persistent Chrome profile), one classification per POST,
requests spaced out — which makes this a BULK-SYNC source in the Overture
sense, never a per-pair fetch lane:

    scripts/sync_cslb.py   drives the browser, downloads one xlsx per
                           classification, parses it here, and stocks
                           PhoneLeadsStore. Pool serving stays pure SQL.
    fetch_license_records  refuses ``cslb_portal`` honestly — the harvest
                           lane never touches a browser.

Shape (verified live 2026-09-14, classification B-2: 1,582 rows, every
row status CLEAR — the portal exports only licenses in good standing):

    LicenseNumber, LastUpdated, BusinessType, BusinessName, Address,
    City, State, Zip, County, PhoneNumber, IssueDate, ExpirationDate,
    Classification, Status

The xlsx is parsed with the stdlib alone (zipfile + ElementTree) — no
openpyxl dependency for a file we fully control the shape of. Business
level only: CSLB publishes no personnel names here, so ``person_name``
is honestly empty (phones-vertical callers work off business + phone).
"""

from __future__ import annotations

import io
import logging
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

logger = logging.getLogger(__name__)

SOURCE_ID = "cslb_portal"
PORTAL_URL = (
    "https://www.cslb.ca.gov/Onlineservices/DataPortal/ListByClassification"
)

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

#: canonical slug -> ((code, official portal label), ...). The LABEL is
#: what lands in ``trade_category`` so ``normalize_trade`` folds it back to
#: the SAME slug at ingest (the round-trip the harvester's tests pin) —
#: never the row's own Classification cell, which mixes "C36"/"C-6"/"D28"
#: formats. Codes/labels extracted from the live form's 78 options
#: (2026-09-14); slugs with no honest CSLB code (lumber, mep) are simply
#: absent, never guessed.
CSLB_CLASSIFICATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "gc": (
        ("B", "General Building Contractor"),
        ("B-2", "Residential Remodeling Contractor"),
    ),
    "mechanical": (
        ("C-4", "Boiler, Hot Water Heating and Steam Fitting Contractor"),
        ("C-20", "Warm-Air Heating, Ventilating and Air-Conditioning "
                 "Contractor"),
    ),
    "drywall": (("C-9", "Drywall Contractor"),),
    "electrical": (("C-10", "Electrical Contractor"),),
    "flooring": (("C-15", "Flooring and Floor Covering Contractors"),),
    "finishes": (("C-6", "Cabinet, Millwork and Finish Carpentry "
                         "Contractor"),),
    "landscaping": (("C-27", "Landscaping Contractor"),),
    "painting": (("C-33", "Painting and Decorating Contractor"),),
    "plumbing": (("C-36", "Plumbing Contractor"),),
    "roofing": (("C-39", "Roofing Contractor"),),
    "concrete": (("C-8", "Concrete Contractor"),),
    "demolition": (("C-21", "Building Moving/Demolition Contractor"),),
}

#: code -> (slug, label) — the sync script's driving map.
CSLB_CODE_MAP: dict[str, tuple[str, str]] = {
    code: (slug, label)
    for slug, pairs in CSLB_CLASSIFICATIONS.items()
    for code, label in pairs
}

#: The xlsx columns we read (verified live); anything else is ignored.
_PHONE_COL = "PhoneNumber"
_NAME_COL = "BusinessName"
_CITY_COL = "City"
_STATE_COL = "State"
_STATUS_COL = "Status"


def parse_xlsx(data: bytes, *, label: str) -> list[dict[str, Any]]:
    """Parse one downloaded ``CSLBSearchData_*.xlsx`` into phone-lead
    records.

    ``label`` is the synced classification's official label — every row in
    the file holds that classification by construction (that is why the
    portal returned it), so it is the honest ``trade_category``. Rows are
    returned as-is; the store's ``add()`` drops bad phones and folds the
    trade (never a silent mangle here).
    """
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")) \
                    .findall(_NS + "si"):
                shared.append("".join(
                    t.text or "" for t in si.iter(_NS + "t")))
        sheet = ET.fromstring(z.read("xl/worksheets/sheet.xml"))
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        logger.warning("cslb parse: not a valid xlsx (%s)", exc)
        return []

    rows = sheet.findall(".//" + _NS + "row")
    header: list[str] | None = None
    records: list[dict[str, Any]] = []
    for row in rows:
        vals: list[str] = []
        for cell in row.findall(_NS + "c"):
            v = cell.find(_NS + "v")
            if v is None:
                vals.append("")
            elif cell.get("t") == "s":
                try:
                    vals.append(shared[int(v.text)])
                except (ValueError, IndexError):
                    vals.append("")
            else:
                vals.append(v.text or "")
        if header is None:
            header = vals
            continue
        d = dict(zip(header, vals, strict=False))
        records.append({
            "phone": d.get(_PHONE_COL, ""),
            "person_name": "",  # CSLB lists businesses only — honest empty
            "business_name": d.get(_NAME_COL, ""),
            "trade_category": label,
            "city": d.get(_CITY_COL, ""),
            "state": d.get(_STATE_COL, "") or "CA",
            "source": SOURCE_ID,
            "license_status": d.get(_STATUS_COL, "").strip(),
            "source_url": PORTAL_URL,
        })
    return records
