"""tabular.py tests — file_shape-driven readers (coverage_engine_v2.md §5).

The adapter's file_shape must be executable: an xlsx read with no
openpyxl (stdlib zipfile + ElementTree), a delimited read honouring
delimiter/encoding/header_row, and a zip container whose inner member is
size-checked BEFORE it is read. Every failure carries an honest reason —
that reason becomes the gate-0 text the registry stores.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.source_scout.tabular import TabularError, read_rows

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _cell(col: str, value: str, *, shared: bool) -> str:
    t = ' t="s"' if shared else ""
    return f'<c r="{col}"{t}><v>{value}</v></c>'


def _xlsx(rows: list[list[str]], *, sheet="xl/worksheets/sheet1.xml",
          use_shared=True) -> bytes:
    """A minimal real xlsx: sharedStrings + one sheet, stdlib-readable."""
    strings: list[str] = []
    body: list[str] = []
    for r_i, row in enumerate(rows, start=1):
        cells: list[str] = []
        for c_i, val in enumerate(row):
            col = chr(ord("A") + c_i)
            if use_shared and not val.isdigit():
                if val not in strings:
                    strings.append(val)
                cells.append(_cell(f"{col}{r_i}", str(strings.index(val)),
                                   shared=True))
            else:
                cells.append(_cell(f"{col}{r_i}", val, shared=False))
        body.append(f'<row r="{r_i}">{"".join(cells)}</row>')

    shared_xml = (
        f'<sst xmlns="{_NS}" count="{len(strings)}" '
        f'uniqueCount="{len(strings)}">'
        + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
    sheet_xml = (
        f'<worksheet xmlns="{_NS}"><sheetData>{"".join(body)}</sheetData>'
        f"</worksheet>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", shared_xml)
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{_NS}"/>')
        z.writestr(sheet, sheet_xml)
    return buf.getvalue()


def _zip_of(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, payload)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# delimited
# ---------------------------------------------------------------------------

def test_csv_reads_header_and_records():
    cols, rows = read_rows(b"Name,Phone\nAcme,555\nBeta,556\n", fmt="csv")
    assert cols == ["Name", "Phone"]
    assert rows == [{"Name": "Acme", "Phone": "555"},
                    {"Name": "Beta", "Phone": "556"}]


def test_tsv_and_semicolon_delimiter_from_file_shape():
    cols, rows = read_rows(b"Name\tPhone\nAcme\t555\n", fmt="tsv")
    assert cols == ["Name", "Phone"] and rows[0]["Phone"] == "555"
    cols, rows = read_rows(b"Name;Phone\nAcme;555\n", fmt="csv",
                           file_shape={"delimiter": ";"})
    assert cols == ["Name", "Phone"] and rows[0]["Name"] == "Acme"


def test_header_row_skips_preamble_but_keeps_body():
    data = b"CSLB public data\nGenerated 2026-09-17\nName,Phone\nAcme,555\n"
    cols, rows = read_rows(data, fmt="csv", file_shape={"header_row": 3})
    assert cols == ["Name", "Phone"]
    assert len(rows) == 1 and rows[0]["Name"] == "Acme"


def test_encoding_from_file_shape():
    data = "Name,City\nAcme,São Paulo\n".encode("cp1252", errors="replace")
    with pytest.raises(TabularError, match="cannot decode as 'utf-8'"):
        read_rows(data, fmt="csv")
    _, rows = read_rows(data, fmt="csv", file_shape={"encoding": "cp1252"})
    assert rows[0]["City"].startswith("S")


def test_blank_lines_and_short_rows_are_tolerated():
    cols, rows = read_rows(b"Name,Phone\n\nAcme,555\nBeta\n", fmt="csv")
    assert cols == ["Name", "Phone"]
    assert rows == [{"Name": "Acme", "Phone": "555"},
                    {"Name": "Beta", "Phone": ""}]


def test_max_rows_caps_the_sample():
    data = b"Name\n" + b"".join(b"Co%d\n" % i for i in range(100))
    _, rows = read_rows(data, fmt="csv", max_rows=5)
    assert len(rows) == 5


def test_no_header_row_is_an_honest_error():
    with pytest.raises(TabularError, match="no header row"):
        read_rows(b"", fmt="csv", file_shape={"header_row": 3})


def test_unsupported_format_is_rejected():
    with pytest.raises(TabularError, match="unsupported format"):
        read_rows(b"x", fmt="parquet")


# ---------------------------------------------------------------------------
# xlsx
# ---------------------------------------------------------------------------

def test_xlsx_reads_shared_strings_and_inline_numbers():
    data = _xlsx([["BusinessName", "BusinessPhone", "State"],
                  ["Acme Corp", "5550001", "CA"],
                  ["Beta Inc", "5550002", "CA"]])
    cols, rows = read_rows(data, fmt="xlsx")
    assert cols == ["BusinessName", "BusinessPhone", "State"]
    assert rows[1] == {"BusinessName": "Beta Inc",
                       "BusinessPhone": "5550002", "State": "CA"}


def test_xlsx_header_row_and_explicit_sheet_inner_path():
    data = _xlsx([["junk", "junk"],
                  ["BusinessName", "BusinessPhone"],
                  ["Acme", "555"]], sheet="xl/worksheets/sheet9.xml")
    cols, rows = read_rows(data, fmt="xlsx", file_shape={
        "header_row": 2, "inner_path": "xl/worksheets/sheet9.xml"})
    assert cols == ["BusinessName", "BusinessPhone"]
    assert rows == [{"BusinessName": "Acme", "BusinessPhone": "555"}]


def test_xlsx_missing_sheet_is_honest():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("docProps/app.xml", "<x/>")
    with pytest.raises(TabularError, match="no sheet xml"):
        read_rows(buf.getvalue(), fmt="xlsx")


def test_not_a_zip_is_honest():
    with pytest.raises(TabularError, match="not a valid xlsx"):
        read_rows(b"<html>portal error page</html>", fmt="xlsx")


# ---------------------------------------------------------------------------
# zip containers + the bomb guard
# ---------------------------------------------------------------------------

def test_zip_csv_reads_the_inner_member():
    data = _zip_of("2026/cslb_export.csv", b"Name,Phone\nAcme,555\n")
    cols, rows = read_rows(data, fmt="zip/csv",
                           file_shape={"inner_path": "cslb_export.csv"})
    assert cols == ["Name", "Phone"] and rows[0]["Name"] == "Acme"


def test_zip_without_inner_path_is_refused():
    data = _zip_of("a.csv", b"Name\nAcme\n")
    with pytest.raises(TabularError, match="needs file_shape.inner_path"):
        read_rows(data, fmt="zip/csv")


def test_zip_inner_path_absent_from_archive_names_the_members():
    data = _zip_of("a.csv", b"Name\nAcme\n")
    with pytest.raises(TabularError, match="not in zip"):
        read_rows(data, fmt="zip/csv", file_shape={"inner_path": "b.csv"})


def test_zip_bomb_inner_file_is_refused_before_reading():
    data = _zip_of("big.csv", b"Name\n" + b"Acme,555\n" * 40_000)
    with pytest.raises(TabularError, match="exceeds zip_bomb_max_mb"):
        read_rows(data, fmt="zip/csv",
                  file_shape={"inner_path": "big.csv",
                              "zip_bomb_max_mb": 0.01})


def test_payload_size_guard_refuses_oversized_outer_bytes():
    with pytest.raises(TabularError, match="payload .* exceeds"):
        read_rows(b"Name\n" + b"x" * 5000, fmt="csv",
                  file_shape={"zip_bomb_max_mb": 0.001})
