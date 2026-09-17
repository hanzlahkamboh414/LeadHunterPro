"""Raw tabular readers — the file_shape half of the adapter (§5).

The adapter's ``file_shape`` is the half that keeps adapters from rotting
(encoding / delimiter / inner path / header row must be explicit). This
module is the ONLY place that turns fetched BYTES into (columns, rows) —
raw source columns, never canonical fields: the adapter's ``field_map``
does the mapping and the Validator measures it.

Reuse note (§14): :mod:`app.phones.cslb` already parses the CSLB xlsx
with the stdlib, but it maps straight to the phone-lead record shape.
The V2 engine needs the RAW columns (field_map's input) and must handle
CSV/TXT/ZIP containers as well — hence this generic reader. Both keep
the stdlib-only philosophy (no openpyxl for shapes we control).
"""

from __future__ import annotations

import csv
import io
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_SHEET_DIR = "xl/worksheets/"

#: file_shape defaults — the adapter may override every one of these.
_DEFAULTS: dict[str, Any] = {
    "inner_path": "",
    "delimiter": ",",
    "encoding": "utf-8",
    "header_row": 1,
    "zip_bomb_max_mb": 500,
}

#: format strings the adapter contract may use.
_DELIMITED = ("csv", "tsv", "txt", "zip/csv", "zip/tsv", "zip/txt")
_XLSX = ("xlsx", "zip/xlsx", "excel")


class TabularError(ValueError):
    """The bytes do not yield typed rows under this file_shape."""


def _shape(file_shape: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(_DEFAULTS)
    for k, v in (file_shape or {}).items():
        if v is not None and v != "":
            out[k] = v
    return out


def _max_bytes(shape: dict[str, Any]) -> int:
    try:
        mb = float(shape.get("zip_bomb_max_mb", 500))
    except (TypeError, ValueError):
        mb = 500.0
    return int(mb * 1024 * 1024)


def _extract(data: bytes, inner_path: str, limit: int) -> bytes:
    """One member out of a zip container, size-checked BEFORE reading."""
    if not inner_path:
        raise TabularError("zip payload needs file_shape.inner_path")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise TabularError(f"not a zip payload: {exc}") from exc
    names = z.namelist()
    match = inner_path if inner_path in names else next(
        (n for n in names if n.endswith(inner_path)), "")
    if not match:
        raise TabularError(
            f"inner_path {inner_path!r} not in zip "
            f"(has: {', '.join(names[:5])})")
    info = z.getinfo(match)
    if info.file_size > limit:
        raise TabularError(
            f"inner file {info.file_size} bytes exceeds zip_bomb_max_mb")
    return z.read(match)


def _assemble(rows: Any, header_row: int, max_rows: int
              ) -> tuple[list[str], list[dict[str, str]]]:
    """Header + records from a row iterator (1-based header_row)."""
    header: list[str] = []
    out: list[dict[str, str]] = []
    for idx, row in enumerate(rows, start=1):
        if idx < header_row:
            continue
        if not header:
            header = [str(c).strip() for c in row]
            continue
        if not any(str(c).strip() for c in row):
            continue  # blank spacer line
        out.append({
            header[i]: (str(row[i]) if i < len(row) else "")
            for i in range(len(header))
        })
        if max_rows and len(out) >= max_rows:
            break
    if not header:
        raise TabularError("no header row found")
    return header, out


def _read_delimited(data: bytes, shape: dict[str, Any], max_rows: int,
                    zip_outer: bool) -> tuple[list[str], list[dict[str, str]]]:
    raw = (_extract(data, str(shape["inner_path"]), _max_bytes(shape))
           if zip_outer else data)
    enc = str(shape["encoding"])
    try:
        text = raw.decode(enc)
    except (UnicodeDecodeError, LookupError) as exc:
        raise TabularError(f"cannot decode as {enc!r}: {exc}") from exc
    reader = csv.reader(io.StringIO(text), delimiter=str(shape["delimiter"]))
    return _assemble(reader, int(shape["header_row"]), max_rows)


def _xlsx_row_values(row: ET.Element, shared: list[str]) -> list[str]:
    vals: list[str] = []
    for cell in row.findall(_XLSX_NS + "c"):
        v = cell.find(_XLSX_NS + "v")
        if v is None:
            vals.append("")
        elif cell.get("t") == "s":
            try:
                vals.append(shared[int(v.text)])
            except (ValueError, IndexError, TypeError):
                vals.append("")
        else:
            vals.append(v.text or "")
    return vals


def _read_xlsx(data: bytes, shape: dict[str, Any], max_rows: int
               ) -> tuple[list[str], list[dict[str, str]]]:
    limit = _max_bytes(shape)
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        sheet_path = str(shape["inner_path"]) or next(
            (n for n in sorted(names)
             if n.startswith(_XLSX_SHEET_DIR) and n.endswith(".xml")), "")
        if not sheet_path or sheet_path not in names:
            raise TabularError(
                f"no sheet xml in xlsx (has: {', '.join(names[:5])})")
        if z.getinfo(sheet_path).file_size > limit:
            raise TabularError("sheet xml exceeds zip_bomb_max_mb")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")) \
                    .findall(_XLSX_NS + "si"):
                shared.append("".join(
                    t.text or "" for t in si.iter(_XLSX_NS + "t")))
        sheet = ET.fromstring(z.read(sheet_path))
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise TabularError(f"not a valid xlsx: {exc}") from exc
    rows = (_xlsx_row_values(r, shared)
            for r in sheet.findall(".//" + _XLSX_NS + "row"))
    return _assemble(rows, int(shape["header_row"]), max_rows)


def read_rows(data: bytes, *, fmt: str, file_shape: dict[str, Any] | None = None,
              max_rows: int = 0) -> tuple[list[str], list[dict[str, str]]]:
    """(columns, rows) from raw bytes under one file_shape.

    ``fmt`` is the adapter's ``fetch.format`` (``csv``/``xlsx``/
    ``zip/csv``/…). ``max_rows=0`` reads everything; a cap is what the
    dry run and the AI sample use. Raises :class:`TabularError` with an
    honest reason — the caller records that as a gate-0 failure.
    """
    shape = _shape(file_shape)
    if len(data) > _max_bytes(shape):
        raise TabularError(
            f"payload {len(data)} bytes exceeds zip_bomb_max_mb")
    fmt = (fmt or "").strip().lower()
    if fmt.endswith("tsv") and not (file_shape or {}).get("delimiter"):
        shape["delimiter"] = "\t"  # the format IS the delimiter here
    if fmt in _XLSX:
        return _read_xlsx(data, shape, max_rows)
    if fmt in _DELIMITED:
        return _read_delimited(data, shape, max_rows, fmt.startswith("zip/"))
    raise TabularError(f"unsupported format {fmt!r}")