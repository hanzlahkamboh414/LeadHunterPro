"""P3 — SODA connector contracts (hermetic: the shared fetch is faked).

The real datasets were live-verified 2026-09-13 (field names, trade
vocabularies, counts). These tests pin the QUERY CONSTRUCTION and the
record PARSING against those verified shapes so a source-side vocabulary
change surfaces here, not as silent zero-yield in production.
"""

from __future__ import annotations

from app.discovery.sources.status import SourceStatus
from app.phones import soda
from app.phones.soda import (
    TRADE_COVERAGE,
    WA_TRADE_VALUES,
    covered_sources,
    fetch_license_records,
)


# ---------------------------------------------------------------------------
# coverage map — which (trade, state) each source honestly serves
# ---------------------------------------------------------------------------

def test_coverage_wa_trades():
    assert covered_sources("gc") == ["wa_license"]
    assert covered_sources("drywall") == ["wa_license"]
    assert covered_sources("demolition") == ["wa_license"]


def test_coverage_tx_trades():
    assert covered_sources("electrical") == ["tdlr_license"]


def test_coverage_mechanical_spans_both_states():
    """mechanical (HVAC) is served by WA specialties AND TDLR A/C."""
    assert TRADE_COVERAGE["mechanical"] == {
        "WA": "wa_license", "TX": "tdlr_license",
    }


def test_coverage_state_filter():
    assert covered_sources("gc", "WA") == ["wa_license"]
    assert covered_sources("gc", "TX") == []  # TX does not license GCs
    assert covered_sources("electrical", "WA") == []  # WA: separate program
    assert covered_sources("electrical", "TX") == ["tdlr_license"]


def test_coverage_unsold_trades_are_honest_empty():
    """Trades no license board covers (lumber, finishes, mep...) return NO
    source — never a fake fetch."""
    assert covered_sources("lumber") == []
    assert covered_sources("finishes") == []


# ---------------------------------------------------------------------------
# query construction
# ---------------------------------------------------------------------------

def test_wa_where_quotes_and_escapes():
    where = soda._wa_where("mechanical", "")
    # Both HVAC specialty values, SoQL-quoted:
    assert "'Heating/Vent/Air-Conditioning and Refrig (HVAC/R)'" in where
    assert "'HVAC/RFRG'" in where
    assert "contractorlicensestatus='ACTIVE'" in where


def test_wa_where_city_prefix_match():
    where = soda._wa_where("gc", "vancouver")
    assert "city LIKE 'VANCOUVER%'" in where


def test_soql_quote_doubles_apostrophes():
    assert soda._soql_quote("O'BRIEN") == "O''BRIEN"


# ---------------------------------------------------------------------------
# record parsing (verified live shapes)
# ---------------------------------------------------------------------------

WA_ROW = {
    "businessname": "!ECO STAR C G CONSTRUCTION LLC",
    "city": "VANCOUVER", "state": "WA",
    "phonenumber": "5039573452",
    "specialtycode1desc": "GENERAL",
    "primaryprincipalname": "GUERRERO MARTINEZ, CARLOS I.",
    "contractorlicensestatus": "ACTIVE",
}

TDLR_ROW = {
    "license_type": "Electrical Contractor",
    "business_name": "INFINITE POWER LLC",
    "business_city_state_zip": "BUDA TX 78610-4477",
    "business_telephone": "5125637173",
    "owner_telephone": "5125637173",
    "owner_name": "INFINITE POWER LLC",
    "license_expiration_date_mmddccyy": "10/06/2026",
}


def test_parse_wa_row():
    rec = soda._parse_wa_row(WA_ROW)
    assert rec["phone"] == "5039573452"
    assert rec["person_name"] == "GUERRERO MARTINEZ, CARLOS I."
    assert rec["trade_category"] == "GENERAL"
    assert rec["state"] == "WA"
    assert rec["source"] == "wa_license"
    assert rec["license_status"] == "ACTIVE"


def test_parse_tdlr_row_active_and_expired():
    rec = soda._parse_tdlr_row(TDLR_ROW)
    assert rec["phone"] == "5125637173"
    assert rec["city"] == "BUDA" and rec["state"] == "TX"
    assert rec["license_status"] == "ACTIVE"
    expired = dict(TDLR_ROW, license_expiration_date_mmddccyy="01/01/2020")
    assert soda._parse_tdlr_row(expired)["license_status"] == "EXPIRED"
    garbage = dict(TDLR_ROW, license_expiration_date_mmddccyy="")
    assert soda._parse_tdlr_row(garbage)["license_status"] == ""


# ---------------------------------------------------------------------------
# fetch plumbing (shared _http.fetch faked — hermetic)
# ---------------------------------------------------------------------------

def test_fetch_license_records_wa(monkeypatch):
    calls = {}

    def _fake_fetch(url, params=None, headers=None, timeout=0.0):
        calls["url"], calls["params"] = url, params
        class _R:
            status = SourceStatus.SUCCESS
            text = '[{"businessname": "B", "phonenumber": "5031110001", ' \
                   '"specialtycode1desc": "GENERAL", "city": "SEATTLE", ' \
                   '"state": "WA", "primaryprincipalname": "X, Y", ' \
                   '"contractorlicensestatus": "ACTIVE"}]'
        return _R()

    monkeypatch.setattr(soda, "fetch", _fake_fetch)
    status, records, meta = fetch_license_records(
        "wa_license", "gc", city="seattle", limit=50,
    )
    assert status == SourceStatus.SUCCESS
    assert len(records) == 1 and records[0]["trade_category"] == "GENERAL"
    assert calls["url"] == soda.WA_DATASET
    assert "GENERAL" in calls["params"]["$where"]
    assert "SEATTLE%" in calls["params"]["$where"]
    assert calls["params"]["$limit"] == "50"
    assert meta["rows_fetched"] == 1


def test_fetch_license_records_unavailable_degrades(monkeypatch):
    """A dead source returns UNAVAILABLE + an honest reason — never raises.
    Hermetic: the pinned-IP retry is disabled and the primary fetch faked."""
    monkeypatch.setattr(soda, "TDLR_PINNED_IP", "")

    def _fake_fetch(url, params=None, headers=None, timeout=0.0):
        class _R:
            status = SourceStatus.UNAVAILABLE
            text = ""
            error = "connection_error: dns"
        return _R()

    monkeypatch.setattr(soda, "fetch", _fake_fetch)
    status, records, meta = fetch_license_records("tdlr_license", "electrical")
    assert status == SourceStatus.UNAVAILABLE
    assert records == []
    assert "error" in meta


def test_pinned_ip_retry_is_last_resort(monkeypatch):
    """DNS failure -> ONE pinned-IP retry; if that also fails the source is
    honestly UNAVAILABLE. The retry itself is faked (hermetic)."""
    calls = {"n": 0}

    def _fake_fetch(url, params=None, headers=None, timeout=0.0):
        class _R:
            status = SourceStatus.UNAVAILABLE
            text = ""
            error = "connection_error: dns"
        return _R()

    import requests

    def _fake_get(*a, **k):
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("pinned host unreachable")

    monkeypatch.setattr(soda, "fetch", _fake_fetch)
    monkeypatch.setattr(requests, "get", _fake_get)
    status, records, meta = fetch_license_records("tdlr_license", "electrical")
    assert status == SourceStatus.UNAVAILABLE
    assert calls["n"] == 1  # exactly one retry, never a loop
    assert meta["error"].startswith("pinned_retry_failed")


def test_fetch_license_records_unknown_source():
    status, records, meta = fetch_license_records("nope", "gc")
    assert status == SourceStatus.ERROR
    assert "unknown_source" in meta["error"]
