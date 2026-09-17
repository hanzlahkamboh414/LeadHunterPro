"""bulk_fetch tests — the stored fetch_spec executor (coverage_engine_v2.md §5).

Pinned against httpx MockTransport: a plain GET, and the ASP.NET WebForms
replay the CA CSLB portal needs (GET page → carry hidden viewstate into
the postback → xlsx). A 200 that is an HTML rejection page must raise,
never be handed to the parser as data.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from app.source_scout.bulk_fetch import (
    BulkFetchError,
    fetch_bytes,
    form_facts,
    _hidden_fields,
)
from app.source_scout.tabular import read_rows

_XLSX_BYTES = b"PK\x03\x04fake-xlsx-container"

_CSLB_PAGE = """
<html><body><form method="post" action="/OnlineServices/CheckLicenseII/..."
<input type="hidden" name="__VIEWSTATE" value="VS&amp;1" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="GEN1" />
<input type="hidden" name="__EVENTVALIDATION" value="EV1" />
<select name="ctl00$MainContent$lbClassification">
  <option value="B-2">B-2 General Building</option></select>
<input type="submit" name="ctl00$MainContent$btnSearch" value="Download" />
</form></body></html>
"""


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _xlsx_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml",
                   '<sst xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main"><si><t>Name</t></si>'
                   "<si><t>Acme</t></si></sst>")
        z.writestr("xl/worksheets/sheet1.xml",
                   '<worksheet xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main"><sheetData><row r="1">'
                   '<c r="A1" t="s"><v>0</v></c></row><row r="2">'
                   '<c r="A2" t="s"><v>1</v></c></row>'
                   "</sheetData></worksheet>")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# plain GET
# ---------------------------------------------------------------------------

def test_get_returns_the_payload():
    def handler(request):
        assert request.headers["user-agent"].startswith("LeadHunterPro")
        return httpx.Response(200, content=_XLSX_BYTES)

    got = fetch_bytes({"method": "GET", "url": "https://x/board.xlsx",
                       "format": "xlsx"}, transport=_transport(handler))
    assert got == _XLSX_BYTES


def test_get_html_rejection_page_is_a_failure_not_data():
    def handler(request):
        return httpx.Response(200, content=b"<!DOCTYPE html><html>ERROR 504"
                              .ljust(60))

    with pytest.raises(BulkFetchError, match="html page"):
        fetch_bytes({"method": "GET", "url": "https://x/board.xlsx",
                     "format": "xlsx"}, transport=_transport(handler))


def test_get_non_200_is_a_failure():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(BulkFetchError, match="HTTP 503"):
        fetch_bytes({"method": "GET", "url": "https://x/board.csv",
                     "format": "csv"}, transport=_transport(handler))


def test_transport_error_is_reported_by_type():
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(BulkFetchError, match="transport: ConnectTimeout"):
        fetch_bytes({"method": "GET", "url": "https://x/a.csv",
                     "format": "csv"}, transport=_transport(handler))


def test_missing_url_and_unknown_method_are_refused():
    with pytest.raises(BulkFetchError, match="no url"):
        fetch_bytes({"format": "csv"})
    with pytest.raises(BulkFetchError, match="unsupported method"):
        fetch_bytes({"method": "PUT", "url": "https://x/a.csv",
                     "format": "csv"})


# ---------------------------------------------------------------------------
# WebForms replay — the CSLB flow
# ---------------------------------------------------------------------------

def test_post_replays_viewstate_and_selects_the_code():
    seen: dict[str, str] = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text=_CSLB_PAGE)
        seen.update(dict(
            pair.split("=", 1) for pair in request.content.decode().split("&")))
        seen["_referer"] = request.headers.get("referer", "")
        return httpx.Response(200, content=_XLSX_BYTES)

    spec = {"method": "POST", "url": "https://www.cslb.ca.gov/portal",
            "format": "xlsx",
            "form": {"select_field": "ctl00$MainContent$lbClassification",
                     "code": "B-2",
                     "submit_field": "ctl00$MainContent$btnSearch",
                     "submit_value": "Download"}}
    assert fetch_bytes(spec, transport=_transport(handler)) == _XLSX_BYTES
    assert seen["__VIEWSTATE"] == "VS%261"  # html-unescaped then url-encoded
    assert seen["__VIEWSTATEGENERATOR"] == "GEN1"
    assert seen["__EVENTVALIDATION"] == "EV1"
    assert seen["ctl00%24MainContent%24lbClassification"] == "B-2"
    assert seen["__EVENTTARGET"] == "ctl00%24MainContent%24btnSearch"
    assert seen["ctl00%24MainContent%24btnSearch"] == "Download"
    assert seen["_referer"].endswith("/portal")


def test_post_without_hidden_state_is_refused():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text="<html><body>no form</body></html>")
        raise AssertionError("must not post without viewstate")

    with pytest.raises(BulkFetchError, match="no hidden form state"):
        fetch_bytes({"method": "POST", "url": "https://x/p", "format": "xlsx",
                     "form": {"select_field": "s", "code": "B-2"}},
                    transport=_transport(handler))


def test_post_form_spec_must_select_something():
    with pytest.raises(BulkFetchError, match="select_field"):
        fetch_bytes({"method": "POST", "url": "https://x/p", "format": "xlsx",
                     "form": {"code": "B-2"}},
                    transport=_transport(
                        lambda r: httpx.Response(200, text=_CSLB_PAGE)))
    with pytest.raises(BulkFetchError, match="select_field"):
        fetch_bytes({"method": "POST", "url": "https://x/p", "format": "xlsx",
                     "form": {"select_field": "ctl", "code": ""}},
                    transport=_transport(
                        lambda r: httpx.Response(200, text=_CSLB_PAGE)))


def test_post_rejection_page_after_the_postback_raises():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text=_CSLB_PAGE)
        return httpx.Response(200, content=b"<html>The requested URL was "
                                            b"rejected.</html>")

    with pytest.raises(BulkFetchError, match="html page"):
        fetch_bytes({"method": "POST", "url": "https://x/p", "format": "xlsx",
                     "form": {"select_field": "ctl", "code": "B-2",
                              "submit_field": "btn"}},
                    transport=_transport(handler))


def test_post_form_page_non_200_raises_before_posting():
    def handler(request):
        return httpx.Response(403, text="waf")

    with pytest.raises(BulkFetchError, match="form page HTTP 403"):
        fetch_bytes({"method": "POST", "url": "https://x/p", "format": "xlsx",
                     "form": {"select_field": "ctl", "code": "B-2"}},
                    transport=_transport(handler))


# ---------------------------------------------------------------------------
# fetch → read integration (the seam dryrun.py uses)
# ---------------------------------------------------------------------------

def test_fetched_xlsx_flows_straight_into_read_rows():
    payload = _xlsx_zip()

    def handler(request):
        return httpx.Response(200, content=payload)

    data = fetch_bytes({"method": "GET", "url": "https://x/b.xlsx",
                        "format": "xlsx"}, transport=_transport(handler))
    cols, rows = read_rows(data, fmt="xlsx")
    assert cols == ["Name"] and rows == [{"Name": "Acme"}]


def test_hidden_fields_ignores_non_hidden_inputs():
    page = ('<input type="text" name="q" value="x">'
            '<input type="hidden" name="__VIEWSTATE" value="v">'
            '<input type="HIDDEN" value="2" name="__EVENTVALIDATION">')
    assert _hidden_fields(page) == {"__VIEWSTATE": "v",
                                    "__EVENTVALIDATION": "2"}


# ---------------------------------------------------------------------------
# form_facts — the real controls, read off the live page
# ---------------------------------------------------------------------------

def test_form_facts_reads_selects_options_and_submits():
    def handler(request):
        return httpx.Response(200, text=_CSLB_PAGE)

    facts = form_facts("https://www.cslb.ca.gov/portal",
                       transport=_transport(handler))
    assert facts["hidden"] == ["__EVENTVALIDATION", "__VIEWSTATE",
                               "__VIEWSTATEGENERATOR"]
    assert facts["selects"] == {
        "ctl00$MainContent$lbClassification": ["B-2"]}
    assert facts["submits"] == [{"name": "ctl00$MainContent$btnSearch",
                                 "value": "Download"}]


def test_form_facts_page_without_controls_is_refused():
    def handler(request):
        return httpx.Response(200, text="<html><body>nothing here</body>")

    with pytest.raises(BulkFetchError, match="no form controls"):
        form_facts("https://x/p", transport=_transport(handler))


def test_form_facts_non_200_is_a_failure():
    with pytest.raises(BulkFetchError, match="form page HTTP 403"):
        form_facts("https://x/p", transport=_transport(
            lambda r: httpx.Response(403)))
