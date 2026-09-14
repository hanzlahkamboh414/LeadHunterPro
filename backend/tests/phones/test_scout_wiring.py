"""P9 sub-inc 5 — phones-lane scout wiring contracts.

The hand WA/TDLR connectors stay the seed coverage; PROMOTED scout sources
extend it through ``effective_trade_coverage`` (hand wins per pair), serve
rows via the payload's PROVEN column mapping, and feed the production
circuit breaker. The repo-wide autouse fixture pins ``soda._scout_store``
to None; these tests restore/inject it explicitly.
"""

from __future__ import annotations

import json
import os

from app.discovery.sources._http import FetchResult
from app.discovery.sources.status import SourceStatus
from app.phones import soda as soda_mod
from app.phones.soda import (
    TRADE_COVERAGE,
    covered_sources,
    effective_trade_coverage,
    fetch_license_records,
    state_sources,
)
from app.source_scout.store import ScoutStore

# The REAL seam, captured at import (the autouse fixture patches
# soda._scout_store to None before every test).
_REAL_SCOUT_STORE = soda_mod._scout_store

_ENDPOINT = "https://data.oregon.gov/resource/pzjw-h5rt.json"

#: A complete, honest proposal payload (the shape the sanitizer requires
#: plus the optional city/business columns).
OR_PAYLOAD = {
    "state": "OR",
    "trade_column": "license_type",
    "trade_values": {"electrical": ["Electrical Contractor"]},
    "phone_column": "phone_number",
    "person_column": "primary_principal_name",
    "status_column": "license_status",
    "city_column": "business_city",
}


def _promoted_store(tmp_path, source_id="or_ccb_license", payload=None,
                    endpoint=_ENDPOINT):
    """A scout store whose one source completed the full trust ladder."""
    store = ScoutStore(db_path=str(tmp_path / "source_scout.db"))
    store.propose(source_id, "soda", "OR CCB", endpoint,
                  payload or dict(OR_PAYLOAD), "playbook")
    store.mark_verified(source_id)
    store.start_probation(source_id)
    for _ in range(5):
        store.record_verdict(source_id, "probation", True, "ok")
    store.promote(source_id)
    return store


def _use_store(monkeypatch, store):
    monkeypatch.setattr(soda_mod, "_scout_store", lambda: store)


def _fake_fetch(rows, status=SourceStatus.SUCCESS, error=""):
    """Hermetic stand-in for the module-level ``soda.fetch``."""
    calls = []

    def fetch(url, params=None, timeout=0.0, **_kw):
        calls.append((url, dict(params or {})))
        ok = status is SourceStatus.SUCCESS
        return FetchResult(
            status=status,
            http_status=200 if ok else 0,
            text=json.dumps(rows) if ok else "",
            error="" if ok else error,
        )

    return fetch, calls


# ---------------------------------------------------------------------------
# coverage merge — hand wins, scout fills gaps
# ---------------------------------------------------------------------------

def test_hand_connectors_win_over_scout(tmp_path, monkeypatch):
    """A scout source claiming WA general-contracting must NOT shadow the
    hand-verified WA connector on its own (trade, state) pair."""
    store = _promoted_store(
        tmp_path, payload={**OR_PAYLOAD, "state": "WA",
                           "trade_values": {"gc": ["General Contractor"]}})
    _use_store(monkeypatch, store)

    eff = effective_trade_coverage()
    assert eff["gc"]["WA"] == "wa_license"
    assert "or_ccb_license" not in eff["gc"].values()


def test_scout_fills_hand_uncovered_pairs(tmp_path, monkeypatch):
    store = _promoted_store(tmp_path)  # electrical/OR — hand coverage lacks it
    _use_store(monkeypatch, store)

    eff = effective_trade_coverage()
    assert eff["electrical"]["OR"] == "or_ccb_license"
    assert covered_sources("electrical", "OR") == ["or_ccb_license"]
    assert "or_ccb_license" in state_sources("OR")
    # The hand map itself is untouched — the merge is a read-side view.
    assert TRADE_COVERAGE.get("electrical", {}).get("OR") is None


def test_without_scout_db_coverage_is_hand_only():
    """The autouse fixture's store=None is the no-scout-db production
    state: coverage is exactly the hand map, nothing invented."""
    assert effective_trade_coverage() == TRADE_COVERAGE


# ---------------------------------------------------------------------------
# the store seam — existence-checked, never file-creating
# ---------------------------------------------------------------------------

def test_scout_store_is_none_and_creates_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(soda_mod, "_scout_store", _REAL_SCOUT_STORE)
    missing = str(tmp_path / "never_created.db")
    monkeypatch.setattr(soda_mod, "_scout_db_path", lambda: missing)

    assert soda_mod._scout_store() is None
    assert not os.path.exists(missing)


def test_scout_store_opens_an_existing_db(tmp_path, monkeypatch):
    _promoted_store(tmp_path)  # creates the file via ScoutStore
    monkeypatch.setattr(soda_mod, "_scout_store", _REAL_SCOUT_STORE)
    monkeypatch.setattr(soda_mod, "_scout_db_path",
                        lambda: str(tmp_path / "source_scout.db"))

    opened = soda_mod._scout_store()
    assert opened is not None
    assert opened.get("or_ccb_license")["status"] == "promoted"


# ---------------------------------------------------------------------------
# serving — the scout fetch branch
# ---------------------------------------------------------------------------

def test_fetch_scout_source_serves_parsed_rows(tmp_path, monkeypatch):
    store = _promoted_store(tmp_path)
    _use_store(monkeypatch, store)
    rows = [{"phone_number": "555-1234",
             "primary_principal_name": "Smith, John",
             "license_type": "Electrical Contractor",
             "license_status": "ACTIVE",
             "business_name": "Acme",
             "business_city": "Portland"}]
    fake, calls = _fake_fetch(rows)
    monkeypatch.setattr(soda_mod, "fetch", fake)

    status, records, meta = fetch_license_records(
        "or_ccb_license", "electrical")

    assert status is SourceStatus.SUCCESS
    url, params = calls[0]
    assert url == _ENDPOINT
    # The $where comes from the payload's PROVEN trade mapping.
    assert params["$where"] == "license_type IN('Electrical Contractor')"
    assert params["$limit"] == "200"
    rec = records[0]
    assert rec["phone"] == "555-1234"
    assert rec["person_name"] == "Smith, John"
    assert rec["trade_category"] == "Electrical Contractor"
    assert rec["business_name"] == "Acme"
    assert rec["city"] == "Portland"
    assert rec["state"] == "OR"
    assert rec["source"] == "or_ccb_license"
    assert rec["license_status"] == "ACTIVE"
    assert rec["source_url"] == _ENDPOINT
    assert meta["rows_fetched"] == 1
    # The fetch fed the production circuit breaker.
    assert store.recent_verdicts("or_ccb_license", "production") == [True]


def test_city_filter_uses_the_payload_city_column(tmp_path, monkeypatch):
    store = _promoted_store(tmp_path)
    _use_store(monkeypatch, store)
    fake, calls = _fake_fetch([])
    monkeypatch.setattr(soda_mod, "fetch", fake)

    fetch_license_records("or_ccb_license", "electrical", city="Portland")

    _, params = calls[0]
    assert "business_city LIKE 'PORTLAND%'" in params["$where"]


def test_trade_the_scout_source_does_not_cover_is_honest(tmp_path,
                                                         monkeypatch):
    store = _promoted_store(tmp_path)  # electrical only
    _use_store(monkeypatch, store)
    fake, calls = _fake_fetch([])
    monkeypatch.setattr(soda_mod, "fetch", fake)

    status, records, meta = fetch_license_records(
        "or_ccb_license", "plumbing")

    assert status is SourceStatus.ERROR
    assert "does not cover trade" in meta["error"]
    assert calls == []  # no fetch was spent on a known-uncovered trade


def test_unknown_source_is_an_honest_error():
    """No scout DB (the autouse state) + no hand match = the same honest
    refusal as ever — never a fake fetch."""
    status, records, meta = fetch_license_records("ghost_source", "gc")
    assert status is SourceStatus.ERROR
    assert "unknown_source" in meta["error"]


def test_quarantine_sources_never_serve(tmp_path, monkeypatch):
    """A source that only reached `verified` (or sits in `proposed`) is
    quarantine by construction — serving it would skip the trust ladder."""
    store = ScoutStore(db_path=str(tmp_path / "source_scout.db"))
    store.propose("or_ccb_license", "soda", "OR CCB", _ENDPOINT,
                  dict(OR_PAYLOAD), "playbook")
    store.mark_verified("or_ccb_license")
    _use_store(monkeypatch, store)
    fake, calls = _fake_fetch([])
    monkeypatch.setattr(soda_mod, "fetch", fake)

    status, records, meta = fetch_license_records(
        "or_ccb_license", "electrical")

    assert status is SourceStatus.ERROR
    assert "unknown_source" in meta["error"]
    assert calls == []


# ---------------------------------------------------------------------------
# the circuit breaker closes the loop
# ---------------------------------------------------------------------------

def test_three_failed_fetches_retire_and_drop_coverage(tmp_path,
                                                       monkeypatch):
    store = _promoted_store(tmp_path)
    _use_store(monkeypatch, store)
    fake, _ = _fake_fetch([], status=SourceStatus.UNAVAILABLE,
                          error="connection_error")
    monkeypatch.setattr(soda_mod, "fetch", fake)

    for _ in range(3):
        status, _, meta = fetch_license_records(
            "or_ccb_license", "electrical")
        assert status is SourceStatus.UNAVAILABLE
        assert "connection_error" in meta["error"]

    # Retired with the honest streak reason…
    row = store.get("or_ccb_license")
    assert row["status"] == "retired"
    assert "consecutive production failures" in row["retire_reason"]
    # …dropped from coverage on the very next lookup (no cache window)…
    assert "or_ccb_license" not in state_sources("OR")
    assert effective_trade_coverage().get("electrical", {}).get("OR") is None
    # …and refused as a source from now on.
    status, _, meta = fetch_license_records("or_ccb_license", "electrical")
    assert status is SourceStatus.ERROR
    assert "unknown_source" in meta["error"]
