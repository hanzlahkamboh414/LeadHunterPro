"""P9 sub-inc 4 — agnes probation contracts: the mechanical gate before
AI spend, verified-only promotion currency, the >=70% auto-promote and
<70% auto-retire with no admin anywhere, honest no-verdict on LLM
errors/unparseable replies, the batch pass, and the production circuit
breaker.
"""

from __future__ import annotations

import json

from app.discovery.sources._http import FetchResult
from app.discovery.sources.status import SourceStatus
from app.source_scout import probation as pb
from app.source_scout.store import ScoutStore


def _store_with_verified(tmp_path):
    """A proposal already through mechanical verify (status=verified)."""
    store = ScoutStore(db_path=str(tmp_path / "source_scout.db"))
    store.propose(
        "or_ccb_license", "soda", "OR CCB",
        "https://data.oregon.gov/resource/pzjw-h5rt.json",
        {"state": "OR", "trade_column": "license_type",
         "trade_values": {"gc": ["General Contractor"]},
         "phone_column": "phone_number",
         "person_column": "primary_principal_name",
         "status_column": "license_status"},
        "playbook")
    store.mark_verified("or_ccb_license")
    return store


def _fetch_ok(rows=5):
    def fetch_fn(url, params=None, timeout=0.0, **_kw):
        params = params or {}
        if "$select" in params:
            return FetchResult(status=SourceStatus.SUCCESS,
                               http_status=200, text='[{"count_1": "5"}]')
        return FetchResult(
            status=SourceStatus.SUCCESS, http_status=200,
            text=json.dumps([
                {"phone_number": "555-1234",
                 "primary_principal_name": "Smith, John",
                 "license_type": "General Contractor",
                 "license_status": "ACTIVE"}
                for _ in range(rows)
            ]))
    return fetch_fn


def _ai_ok(_prompt):
    return "PASS — real person names with phones and trade codes."


def _ai_fail(_prompt):
    return "FAIL — the person column holds company names."


# ---------------------------------------------------------------------------
# one probation check
# ---------------------------------------------------------------------------

def test_first_check_enters_probation_and_records_verdict(tmp_path):
    store = _store_with_verified(tmp_path)

    out = pb.run_probation_check(store, "or_ccb_license",
                                 fetch_fn=_fetch_ok(), ai_ask=_ai_ok)

    assert out["recorded"] is True
    assert out["passed"] is True
    assert out["status"] == "probation"  # 1/5 trials — not enough evidence
    assert store.get("or_ccb_license")["status"] == "probation"
    assert store.probation_score("or_ccb_license") == (1, 1, 1.0)


def test_mechanical_gate_fails_without_ai_spend(tmp_path):
    """A source that cannot serve rows fails BEFORE agnes is asked —
    the AI never spends without a logged fetch."""
    store = _store_with_verified(tmp_path)
    calls = []

    def ai(_prompt):
        calls.append(1)
        return "PASS"

    def dead_fetch(url, params=None, timeout=0.0, **_kw):
        return FetchResult(status=SourceStatus.UNAVAILABLE,
                           error="connection_error: dns")

    out = pb.run_probation_check(store, "or_ccb_license",
                                 fetch_fn=dead_fetch, ai_ask=ai)

    assert out["recorded"] is True
    assert out["passed"] is False
    assert calls == []  # agnes was never asked
    assert store.probation_score("or_ccb_license")[1] == 1


def test_agnes_fail_verdict_counts_against_the_source(tmp_path):
    store = _store_with_verified(tmp_path)
    out = pb.run_probation_check(store, "or_ccb_license",
                                 fetch_fn=_fetch_ok(), ai_ask=_ai_fail)
    assert out["passed"] is False
    assert store.probation_score("or_ccb_license") == (0, 1, 0.0)


def test_llm_error_records_no_verdict(tmp_path):
    """Our AI being down must not fail a source that is serving fine."""
    store = _store_with_verified(tmp_path)

    def dead_ai(_prompt):
        raise RuntimeError("router down")

    out = pb.run_probation_check(store, "or_ccb_license",
                                 fetch_fn=_fetch_ok(), ai_ask=dead_ai)
    assert out["recorded"] is False
    assert "LLM call failed" in out["agnes_reason"]
    assert store.probation_score("or_ccb_license") == (0, 0, 0.0)
    # Still entered probation (the fetch proved it serves) — no verdict.
    assert store.get("or_ccb_license")["status"] == "probation"


def test_unparseable_reply_records_no_verdict(tmp_path):
    store = _store_with_verified(tmp_path)

    def rambling(_prompt):
        return "The data appears to contain various fields of interest."

    out = pb.run_probation_check(store, "or_ccb_license",
                                 fetch_fn=_fetch_ok(), ai_ask=rambling)
    assert out["recorded"] is False
    assert store.probation_score("or_ccb_license") == (0, 0, 0.0)


def test_wrong_status_is_an_honest_skip(tmp_path):
    store = _store_with_verified(tmp_path)
    store.start_probation("or_ccb_license")
    store.promote("or_ccb_license")

    out = pb.run_probation_check(store, "or_ccb_license",
                                 fetch_fn=_fetch_ok(), ai_ask=_ai_ok)
    assert out["recorded"] is False
    assert "nothing to probation" in out["agnes_reason"]


# ---------------------------------------------------------------------------
# auto-promote / auto-retire (no admin anywhere)
# ---------------------------------------------------------------------------

def _run_trials(store, n_pass, n_fail, ai):
    for _ in range(n_pass):
        pb.run_probation_check(store, "or_ccb_license",
                               fetch_fn=_fetch_ok(), ai_ask=ai)
    if n_fail:
        def dead(url, params=None, timeout=0.0, **_kw):
            return FetchResult(status=SourceStatus.UNAVAILABLE,
                               error="timeout")
        for _ in range(n_fail):
            pb.run_probation_check(store, "or_ccb_license",
                                   fetch_fn=dead, ai_ask=ai)


def test_promotion_at_threshold(tmp_path):
    """4/5 = 80% >= 70% → auto-promoted after the minimum trials."""
    store = _store_with_verified(tmp_path)
    _run_trials(store, 4, 1, _ai_ok)
    assert store.get("or_ccb_license")["status"] == "promoted"


def test_retirement_below_threshold(tmp_path):
    """2/5 = 40% < 70% → auto-retired with the honest score in reason."""
    store = _store_with_verified(tmp_path)
    _run_trials(store, 2, 3, _ai_ok)
    row = store.get("or_ccb_license")
    assert row["status"] == "retired"
    assert "2/5" in row["retire_reason"]
    assert "70%" in row["retire_reason"]


def test_fewer_than_min_trials_never_decides(tmp_path):
    """3/4 trials = 75% but below MIN_TRIALS — evidence first, no early
    promotion on a lucky streak."""
    store = _store_with_verified(tmp_path)
    _run_trials(store, 3, 1, _ai_ok)
    assert store.get("or_ccb_license")["status"] == "probation"


# ---------------------------------------------------------------------------
# the batch pass
# ---------------------------------------------------------------------------

def test_probation_pass_covers_verified_and_probation(tmp_path):
    store = _store_with_verified(tmp_path)
    # A second source already mid-probation with one pass.
    store.propose("nv_board", "soda", "NV Board",
                  "https://data.nv.gov/resource/aaa.json",
                  {"state": "NV", "trade_column": "license_type",
                   "trade_values": {"plumbing": ["Plumbing Contractor"]},
                   "phone_column": "phone", "person_column": "owner",
                   "status_column": ""}, "playbook")
    store.mark_verified("nv_board")
    pb.run_probation_check(store, "nv_board", fetch_fn=_fetch_ok(),
                           ai_ask=_ai_ok)

    summary = pb.probation_pass(store, fetch_fn=_fetch_ok(), ai_ask=_ai_ok)

    # or_ccb enters probation with this pass; nv_board was already in it.
    assert summary["started"] == ["or_ccb_license"]
    assert set(summary["checked"]) == {"or_ccb_license", "nv_board"}
    assert summary["promoted"] == []  # 2/5 trials — not enough evidence
    assert summary["retired"] == []
    assert store.get("or_ccb_license")["status"] == "probation"
    assert store.get("nv_board")["status"] == "probation"


def test_probation_pass_reports_llm_skips_honestly(tmp_path):
    store = _store_with_verified(tmp_path)

    def dead_ai(_prompt):
        raise RuntimeError("router down")

    summary = pb.probation_pass(store, fetch_fn=_fetch_ok(), ai_ask=dead_ai)
    assert summary["checked"] == []
    assert "LLM call failed" in summary["skipped"]["or_ccb_license"]


# ---------------------------------------------------------------------------
# the production circuit breaker
# ---------------------------------------------------------------------------

def _promoted(tmp_path):
    store = _store_with_verified(tmp_path)
    store.start_probation("or_ccb_license")
    for _ in range(5):
        store.record_verdict("or_ccb_license", "probation", True, "ok")
    store.promote("or_ccb_license")
    return store


def test_three_consecutive_production_fails_retire(tmp_path):
    store = _promoted(tmp_path)
    assert pb.record_production_outcome(store, "or_ccb_license",
                                        False, "http_500") is False
    assert pb.record_production_outcome(store, "or_ccb_license",
                                        False, "http_500") is False
    assert pb.record_production_outcome(store, "or_ccb_license",
                                        False, "http_500") is True
    row = store.get("or_ccb_license")
    assert row["status"] == "retired"
    assert "3 consecutive production failures" in row["retire_reason"]


def test_a_passing_outcome_resets_the_streak(tmp_path):
    store = _promoted(tmp_path)
    pb.record_production_outcome(store, "or_ccb_license", False, "x")
    pb.record_production_outcome(store, "or_ccb_license", False, "x")
    pb.record_production_outcome(store, "or_ccb_license", True)
    assert store.get("or_ccb_license")["status"] == "promoted"
    pb.record_production_outcome(store, "or_ccb_license", False, "x")
    pb.record_production_outcome(store, "or_ccb_license", False, "x")
    # Streak is 2 since the pass — still promoted.
    assert store.get("or_ccb_license")["status"] == "promoted"


def test_breaker_ignores_non_promoted_sources(tmp_path):
    store = _store_with_verified(tmp_path)  # only verified
    assert pb.record_production_outcome(store, "or_ccb_license",
                                        False, "x") is False
    assert store.get("or_ccb_license")["status"] == "verified"
