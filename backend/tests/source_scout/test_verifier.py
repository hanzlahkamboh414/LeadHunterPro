"""P9 sub-inc 3 — mechanical verifier contracts: error classification
with DNS retry + IP pin + alt endpoint, shape checks against the
PROPOSED columns, trade-evidence proof of the AI's value mapping,
volume/recency numbers, verdict recording, and the lifecycle advance on
pass (fail stays retryable in ``proposed``). All hermetic — the fake
router answers by URL/params shape, never the network.
"""

from __future__ import annotations

import json

from app.discovery.sources._http import FetchResult
from app.discovery.sources.status import SourceStatus
from app.source_scout import verifier as vf
from app.source_scout.store import ScoutStore


_NOW = 1_800_000_000.0  # fixed epoch for deterministic recency math


class FakeFetch:
    """Routes fetch calls by their SODA shape: sample ($limit only),
    filtered ($where), count ($select), metadata (/api/views/).

    ``*_fails`` counts UNAVAILABLE (DNS-flake) responses before the row
    data serves; ``*_error`` makes the call a hard HTTP error instead.
    """

    def __init__(self, *, sample=None, sample_fails=0, sample_error=False,
                 filtered=None, where_fails=0, filtered_error=False,
                 count='[{"count_1": "5000"}]', count_error=False,
                 meta=None, meta_error=False, alt_endpoint_ok=False):
        self.sample = sample if sample is not None else []
        self.sample_fails = sample_fails
        self.sample_error = sample_error
        self.filtered = filtered if filtered is not None else []
        self.where_fails = where_fails
        self.filtered_error = filtered_error
        self.count = count
        self.count_error = count_error
        self.meta = meta if meta is not None else {"rowsUpdatedAt": _NOW}
        self.meta_error = meta_error
        self.alt_endpoint_ok = alt_endpoint_ok
        self.calls = []

    def __call__(self, url, params=None, timeout=0.0, **_kw):
        params = params or {}
        self.calls.append((url, dict(params)))
        if "/api/views/" in url:
            if self.meta_error:
                if self.alt_endpoint_ok:
                    return FetchResult(status=SourceStatus.SUCCESS,
                                       http_status=200, text="{}")
                return FetchResult(status=SourceStatus.ERROR,
                                   http_status=404, error="http_404")
            return FetchResult(status=SourceStatus.SUCCESS,
                               http_status=200, text=json.dumps(self.meta))
        if "$select" in params:
            if self.count_error:
                return FetchResult(status=SourceStatus.ERROR,
                                   http_status=400, error="http_400")
            return FetchResult(status=SourceStatus.SUCCESS,
                               http_status=200, text=self.count)
        if "$where" in params:
            if self.where_fails > 0:
                self.where_fails -= 1
                return FetchResult(status=SourceStatus.UNAVAILABLE,
                                   error="connection_error: dns")
            if self.filtered_error:
                return FetchResult(status=SourceStatus.ERROR,
                                   http_status=500, error="http_500")
            return FetchResult(status=SourceStatus.SUCCESS,
                               http_status=200,
                               text=json.dumps(self.filtered))
        # sample fetch
        if self.sample_fails > 0:
            self.sample_fails -= 1
            return FetchResult(status=SourceStatus.UNAVAILABLE,
                               error="connection_error: dns")
        if self.sample_error:
            return FetchResult(status=SourceStatus.ERROR, http_status=403,
                               error="http_403")
        return FetchResult(status=SourceStatus.SUCCESS, http_status=200,
                           text=json.dumps(self.sample))


def _store_with_proposal(tmp_path, **payload_extra):
    store = ScoutStore(db_path=str(tmp_path / "source_scout.db"))
    payload = {
        "state": "OR",
        "trade_column": "license_type",
        "trade_values": {"gc": ["General Contractor"]},
        "phone_column": "phone_number",
        "person_column": "primary_principal_name",
        "status_column": "license_status",
    }
    payload.update(payload_extra)
    store.propose("or_ccb_license", "soda", "OR CCB",
                  "https://data.oregon.gov/resource/pzjw-h5rt.json",
                  payload, "playbook")
    return store


def _rows(n=10, phone="503-957-3452"):
    """Ten license rows. ``phone`` must be a REAL 10-digit number — the
    shape gate counts only values the serving lane's ``normalize_phone``
    accepts, so a filler like "555-1234" would fail it silently."""
    return [
        {"phone_number": phone, "primary_principal_name": "Smith, John",
         "license_type": "General Contractor", "license_status": "ACTIVE",
         "business_name": f"Acme {i}"}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_full_pass_verifies_and_advances(tmp_path):
    store = _store_with_proposal(tmp_path)
    ff = FakeFetch(sample=_rows(), filtered=_rows(3))

    out = vf.verify_source(store, "or_ccb_license", fetch_fn=ff,
                           clock=lambda: _NOW)

    assert out["passed"] is True
    assert all(c["ok"] for c in out["checks"])
    assert [c["name"] for c in out["checks"]] == [
        "sample_fetch", "sample_nonempty", "column:phone_column",
        "column:person_column", "column:trade_column",
        "column:status_column", "sample_with_phone", "trade_evidence",
        "volume", "recency",
    ]
    assert store.get("or_ccb_license")["status"] == "verified"


def test_mechanical_verdict_recorded_not_probation(tmp_path):
    """Verified-only reward: the mechanical verdict is evidence, but it
    must never count towards the agnes probation score."""
    store = _store_with_proposal(tmp_path)
    vf.verify_source(store, "or_ccb_license",
                     fetch_fn=FakeFetch(sample=_rows(), filtered=_rows(3)),
                     clock=lambda: _NOW)

    assert store.probation_score("or_ccb_license") == (0, 0, 0.0)


# ---------------------------------------------------------------------------
# error classification + fallbacks
# ---------------------------------------------------------------------------

def test_transient_dns_failure_retried_and_passes(tmp_path):
    store = _store_with_proposal(tmp_path)
    ff = FakeFetch(sample=_rows(), filtered=_rows(3), sample_fails=1)

    out = vf.verify_source(store, "or_ccb_license", fetch_fn=ff,
                           clock=lambda: _NOW)
    assert out["passed"] is True


def test_pinned_ip_fallback_used_after_dns_failure(tmp_path):
    store = _store_with_proposal(tmp_path, pinned_ip="52.1.2.3")
    ff = FakeFetch(sample_fails=99, where_fails=99)

    def fake_pinned(url, params, host, ip):
        if "$where" in (params or {}):
            text = json.dumps(_rows(3))
        else:
            text = json.dumps(_rows())
        return FetchResult(status=SourceStatus.SUCCESS, http_status=200,
                           text=text)

    out = vf.verify_source(store, "or_ccb_license", fetch_fn=ff,
                           pinned_get=fake_pinned, clock=lambda: _NOW)
    assert out["passed"] is True
    fetches = [c for c in ff.calls if "/api/views/" not in c[0]]
    assert len(fetches) >= 4  # 2 sample tries, 2 filtered tries — all DNS-dead


def test_http_error_with_dead_alt_endpoint_fails_honestly(tmp_path):
    store = _store_with_proposal(tmp_path)
    ff = FakeFetch(sample_error=True, filtered_error=True,
                   count_error=True, meta_error=True)

    out = vf.verify_source(store, "or_ccb_license", fetch_fn=ff,
                           clock=lambda: _NOW)

    assert out["passed"] is False
    by_name = {c["name"]: c for c in out["checks"]}
    assert "also failing" in by_name["sample_fetch"]["note"]
    # One mechanical FAIL stays proposed. The third consecutive FAIL retires.
    assert store.get("or_ccb_license")["status"] == "proposed"


def _failing_fetch():
    return FakeFetch(sample_error=True, filtered_error=True,
                     count_error=True, meta_error=True)


def test_two_mechanical_fails_stay_proposed(tmp_path):
    """A transient miss is retryable. Probation-stage fails do not count
    toward the mechanical streak."""
    store = _store_with_proposal(tmp_path)
    ff = _failing_fetch()
    for _ in range(2):
        out = vf.verify_source(store, "or_ccb_license", fetch_fn=ff,
                               clock=lambda: _NOW)
        assert out["passed"] is False
    store.record_verdict("or_ccb_license", "probation", False,
                         "other stage")
    assert store.get("or_ccb_license")["status"] == "proposed"


def test_three_consecutive_mechanical_fails_retire(tmp_path):
    """co_cda / CT CE / DE asbestos: the same mechanical FAIL every pass
    becomes terminal, with the check detail stored as retire_reason."""
    store = _store_with_proposal(tmp_path)
    ff = _failing_fetch()
    for _ in range(2):
        vf.verify_source(store, "or_ccb_license", fetch_fn=ff,
                         clock=lambda: _NOW)
    assert store.get("or_ccb_license")["status"] == "proposed"

    out = vf.verify_source(store, "or_ccb_license", fetch_fn=ff,
                           clock=lambda: _NOW)

    row = store.get("or_ccb_license")
    assert out["passed"] is False
    assert row["status"] == "retired"
    assert "sample_fetch=FAIL" in (row.get("retire_reason") or "")
    assert store.recent_verdicts(
        "or_ccb_license", "mechanical") == [False, False, False]
    skip = vf.verify_source(store, "or_ccb_license", fetch_fn=ff,
                            clock=lambda: _NOW)
    assert "nothing to verify" in skip["reason"]
    assert store.get("or_ccb_license")["status"] == "retired"
    assert store.recent_verdicts(
        "or_ccb_license", "mechanical") == [False, False, False]


def test_mechanical_pass_resets_the_fail_streak(tmp_path):
    """A leading pass breaks the streak. A real mechanical PASS leaves
    ``proposed`` via mark_verified, so the reset is locked by writing the
    verdict directly, then two later fails must stay proposed."""
    store = _store_with_proposal(tmp_path)
    ff = _failing_fetch()
    vf.verify_source(store, "or_ccb_license", fetch_fn=ff,
                     clock=lambda: _NOW)
    store.record_verdict("or_ccb_license", "mechanical", True,
                         "injected pass")
    for _ in range(2):
        out = vf.verify_source(store, "or_ccb_license", fetch_fn=ff,
                               clock=lambda: _NOW)
        assert out["passed"] is False
    assert store.get("or_ccb_license")["status"] == "proposed"


def test_http_error_but_live_alt_endpoint_still_fails_shape_honestly(tmp_path):
    """A rescued connection with no sample rows cannot fake a shape pass —
    the honest failure is the empty sample, never an invented pass."""
    store = _store_with_proposal(tmp_path)
    ff = FakeFetch(sample_error=True, filtered_error=True,
                   meta_error=True, alt_endpoint_ok=True)

    out = vf.verify_source(store, "or_ccb_license", fetch_fn=ff,
                           clock=lambda: _NOW)
    assert out["passed"] is False
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["sample_nonempty"]["ok"] is False


# ---------------------------------------------------------------------------
# shape + trade evidence — the AI's claims on trial
# ---------------------------------------------------------------------------

def test_missing_person_column_fails(tmp_path):
    rows = [{"phone_number": "503-957-3452",
             "license_type": "General Contractor"}]
    store = _store_with_proposal(tmp_path)
    out = vf.verify_source(
        store, "or_ccb_license",
        fetch_fn=FakeFetch(sample=rows, filtered=rows),
        clock=lambda: _NOW)
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["column:person_column"]["ok"] is False
    assert out["passed"] is False


def test_claimed_status_column_missing_fails(tmp_path):
    rows = _rows()
    for r in rows:
        del r["license_status"]
    store = _store_with_proposal(tmp_path)
    out = vf.verify_source(
        store, "or_ccb_license",
        fetch_fn=FakeFetch(sample=rows, filtered=rows),
        clock=lambda: _NOW)
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["column:status_column"]["ok"] is False


def test_too_few_rows_with_phone_fails(tmp_path):
    store = _store_with_proposal(tmp_path)
    out = vf.verify_source(
        store, "or_ccb_license",
        fetch_fn=FakeFetch(sample=_rows(n=2), filtered=_rows(3)),
        clock=lambda: _NOW)
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["sample_with_phone"]["ok"] is False


def test_a_metric_column_cannot_be_the_phone_column(tmp_path):
    """The live defect (2026-09-22): ``of_phone_calls_answered_within``
    holds "0.95" on every row, so a non-empty test passed it while the
    serving lane's ``normalize_phone`` returns '' for that value — the
    source would have been promoted holding zero serveable phones."""
    store = _store_with_proposal(
        tmp_path, phone_column="of_phone_calls_answered_within")
    rows = [{"of_phone_calls_answered_within": "0.95",
             "primary_principal_name": "Smith, John",
             "license_type": "General Contractor",
             "license_status": "ACTIVE"}
            for _ in range(10)]
    out = vf.verify_source(
        store, "or_ccb_license",
        fetch_fn=FakeFetch(sample=rows, filtered=rows),
        clock=lambda: _NOW)
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["sample_with_phone"]["ok"] is False
    assert "0 rows with a phone" in by_name["sample_with_phone"]["note"]
    assert out["passed"] is False


def test_junk_values_cannot_inflate_the_phone_count(tmp_path):
    """Two real numbers among twelve rows is still too few: counting
    non-empty cells would let the ten junk values carry the count past
    MIN_SAMPLE_ROWS. Only values that normalize to a phone count."""
    store = _store_with_proposal(tmp_path)
    rows = _rows(n=2) + [
        {"phone_number": "N/A", "primary_principal_name": "Smith, John",
         "license_type": "General Contractor", "license_status": "ACTIVE"}
        for _ in range(10)
    ]
    out = vf.verify_source(
        store, "or_ccb_license",
        fetch_fn=FakeFetch(sample=rows, filtered=_rows(3)),
        clock=lambda: _NOW)
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["sample_with_phone"]["ok"] is False
    assert "2 rows with a phone" in by_name["sample_with_phone"]["note"]


def test_wrong_trade_values_fail_the_mapping(tmp_path):
    """The single most likely AI hallucination: plausible-looking dataset
    values that match nothing. The filtered sample returning zero rows
    must fail the verdict."""
    store = _store_with_proposal(tmp_path)
    out = vf.verify_source(
        store, "or_ccb_license",
        fetch_fn=FakeFetch(sample=_rows(), filtered=[]),
        clock=lambda: _NOW)
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["trade_evidence"]["ok"] is False
    assert "no rows for 'gc'" in by_name["trade_evidence"]["note"]


# ---------------------------------------------------------------------------
# volume + recency
# ---------------------------------------------------------------------------

def test_toy_dataset_fails_volume(tmp_path):
    store = _store_with_proposal(tmp_path)
    out = vf.verify_source(
        store, "or_ccb_license",
        fetch_fn=FakeFetch(sample=_rows(), filtered=_rows(3),
                           count='[{"count_1": "42"}]'),
        clock=lambda: _NOW)
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["volume"]["ok"] is False
    assert "42 rows" in by_name["volume"]["note"]


def test_count_refused_falls_back_to_sample_honestly(tmp_path):
    store = _store_with_proposal(tmp_path)
    out = vf.verify_source(
        store, "or_ccb_license",
        fetch_fn=FakeFetch(sample=_rows(), filtered=_rows(3),
                           count_error=True),
        clock=lambda: _NOW)
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["volume"]["ok"] is True
    assert "count unavailable" in by_name["volume"]["note"]


def test_stale_dataset_fails_recency(tmp_path):
    stale_epoch = _NOW - 500 * 86400  # > MAX_STALENESS_DAYS
    store = _store_with_proposal(tmp_path)
    out = vf.verify_source(
        store, "or_ccb_license",
        fetch_fn=FakeFetch(sample=_rows(), filtered=_rows(3),
                           meta={"rowsUpdatedAt": stale_epoch}),
        clock=lambda: _NOW)
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["recency"]["ok"] is False
    assert "500 days" in by_name["recency"]["note"]


def test_missing_recency_timestamp_is_a_note_not_a_fail(tmp_path):
    store = _store_with_proposal(tmp_path)
    out = vf.verify_source(
        store, "or_ccb_license",
        fetch_fn=FakeFetch(sample=_rows(), filtered=_rows(3),
                           meta={}),
        clock=lambda: _NOW)
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["recency"]["ok"] is True
    assert "unavailable" in by_name["recency"]["note"]


# ---------------------------------------------------------------------------
# lifecycle guards
# ---------------------------------------------------------------------------

def test_non_proposed_status_is_an_honest_skip(tmp_path):
    store = _store_with_proposal(tmp_path)
    store.mark_verified("or_ccb_license")

    out = vf.verify_source(store, "or_ccb_license",
                           fetch_fn=FakeFetch(), clock=lambda: _NOW)
    assert out["passed"] is False
    assert "nothing to verify" in out["reason"]
    # No verdict row was added for the skip.
    _, total, _ = store.probation_score("or_ccb_license")
    assert total == 0


def test_unknown_source_raises(tmp_path):
    store = ScoutStore(db_path=str(tmp_path / "source_scout.db"))
    import pytest
    with pytest.raises(ValueError, match="unknown source_id"):
        vf.verify_source(store, "ghost", fetch_fn=FakeFetch())
