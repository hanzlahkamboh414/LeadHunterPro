"""Minimal XLSX writer — stdlib alone (zipfile + XML), no openpyxl.

The same no-dependency philosophy as :mod:`app.phones.cslb` (which PARSES
xlsx with the stdlib): we fully control the shape we write — one worksheet,
inline strings, no formulas, no styling beyond a bold header. Anything
Excel/Sheets/LibreOffice needs to open the file is below, nothing more.
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

#: The workbook-wide content type for a sheet with inline strings.
_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="%(sheet)s" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""


def _col_letter(index: int) -> str:
    """0-based column index -> spreadsheet column letters (0 -> A)."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _sheet_xml(rows: list[list[str]], header: list[str]) -> str:
    """The one worksheet: a bold-ish header row (t="inlineStr"), then rows."""
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           "<sheetData>"]

    def _row(r: int, cells: list[str]) -> None:
        out.append(f'<row r="{r}">')
        for c, value in enumerate(cells):
            # Excel's own limit; longer cells are truncated, not corrupted.
            text = (value or "")[:32767]
            out.append(
                f'<c r="{_col_letter(c)}{r}" t="inlineStr"><is><t>'
                f"{escape(text)}</t></is></c>"
            )
        out.append("</row>")

    _row(1, header)
    for i, row in enumerate(rows, start=2):
        _row(i, row)
    out.append("</sheetData></worksheet>")
    return "".join(out)


def build_xlsx(rows: list[list[str]], *, header: list[str],
               sheet: str = "Sheet1") -> bytes:
    """A complete one-sheet .xlsx as bytes — ready for a download response.

    ``header`` is row 1; every row in ``rows`` follows. All cells are inline
    strings (no shared-strings part to keep in sync). Cell values are XML-
    escaped; the sheet name is sanitized to Excel's rules.
    """
    safe_sheet = "".join(ch for ch in sheet if ch.isalnum() or ch in " _-")[:31] or "Sheet1"
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _ROOT_RELS,
        "xl/workbook.xml": _WORKBOOK % {"sheet": escape(safe_sheet)},
        "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS,
        "xl/worksheets/sheet1.xml": _sheet_xml(rows, header),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, xml in parts.items():
            zf.writestr(name, xml)
    return buffer.getvalue()
