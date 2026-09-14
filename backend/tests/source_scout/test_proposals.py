"""P9 sub-inc 2 — scout proposal generation contracts: the playbook
prompt (known/dead memory + covered states in, real SODA-shaped
proposals out), the flood guard, the sanitizer's hard shape rules, and
honest zeros (LLM error / garbage reply is a reason, never a crash).
"""

from __future__ import annotations

import json

from app.source_scout import proposals as pg
from app.source_scout.store import ScoutStore


def _store(tmp_path):
    return ScoutStore(db_path=str(tmp_path / "source_scout.db"))


def _fake_ai(reply, prompts=None):
    def ask(prompt):
        if prompts is not None:
            prompts.append(prompt)
        return reply
    return ask


_COVERAGE = {"gc": {"WA": "wa_license"}, "electrical": {"TX": "tdlr_license"}}

_GOOD_REPLY = json.dumps([
    {
        "source_id": "or_ccb_license",
        "name": "Oregon CCB Contractor Licenses",
        "endpoint": "https://data.oregon.gov/resource/pzjw-h5rt.json",
        "state": "OR",
        "trade_column": "license_type",
        "trade_values": {"gc": ["General Contractor"]},
        "phone_column": "phone_number",
        "person_column": "primary_principal_name",
        "status_column": "license_status",
    },
])


# ---------------------------------------------------------------------------
# known/dead memory seeding
# ---------------------------------------------------------------------------

def test_seed_known_is_idempotent(tmp_path):
    store = _store(tmp_path)
    pg.seed_known(store)
    pg.seed_known(store)

    km = store.known_map()
    assert km["wa_license"]["status"] == "good"
    assert km["public_searxng"]["status"] == "dead"
    assert km["cslb_portal"]["status"] == "dead"
    # One row per route, not two.
    assert len(km) == len(pg.KNOWN_GOOD) + len(pg.KNOWN_DEAD)


# ---------------------------------------------------------------------------
# the flood guard
# ---------------------------------------------------------------------------

def test_flood_guard_holds_when_quarantine_full(tmp_path):
    store = _store(tmp_path)
    for i in range(pg.MAX_PENDING):
        store.propose(f"src_{i}", "soda", f"n{i}", "https://x", {}, "t")

    def boom(_prompt):
        raise AssertionError("AI must not be called under the flood guard")

    out = pg.generate_source_proposals(
        store, boom, existing_coverage=_COVERAGE)

    assert out["proposed"] == []
    assert "flood guard" in out["reason"]


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_valid_proposal_lands_in_quarantine(tmp_path):
    store = _store(tmp_path)
    pg.seed_known(store)

    out = pg.generate_source_proposals(
        store, _fake_ai(_GOOD_REPLY), existing_coverage=_COVERAGE)

    assert out["proposed"] == ["or_ccb_license"]
    assert out["reason"] == ""
    row = store.get("or_ccb_license")
    assert row["status"] == "proposed"
    assert row["kind"] == "soda"
    assert row["payload"]["state"] == "OR"
    assert row["payload"]["phone_column"] == "phone_number"
    assert row["payload"]["status_column"] == "license_status"


def test_prompt_carries_coverage_known_routes_and_trades(tmp_path):
    """The playbook must tell the AI WHERE the gaps are (covered states),
    what NOT to re-propose (known routes, good and dead), and the only
    trade vocabulary that exists."""
    store = _store(tmp_path)
    pg.seed_known(store)
    prompts: list[str] = []

    pg.generate_source_proposals(
        store, _fake_ai("[]", prompts), existing_coverage=_COVERAGE)

    assert len(prompts) == 1
    p = prompts[0]
    assert "WA" in p and "wa_license" in p          # covered states listed
    assert "TX" in p and "tdlr_license" in p
    assert "public_searxng" in p                    # dead routes listed
    assert "cslb_portal" in p
    assert "gc" in p and "finishes" in p            # canonical 14 listed
    assert "never invent dataset IDs" in p          # honesty instruction


def test_duplicate_proposal_is_rejected_not_duplicated(tmp_path):
    store = _store(tmp_path)
    out1 = pg.generate_source_proposals(
        store, _fake_ai(_GOOD_REPLY), existing_coverage=_COVERAGE)
    out2 = pg.generate_source_proposals(
        store, _fake_ai(_GOOD_REPLY), existing_coverage=_COVERAGE)

    assert out1["proposed"] == ["or_ccb_license"]
    assert out2["proposed"] == []
    assert "already exists in the store" in out2["rejected"][0]["reason"]
    assert len(store.list_quarantine()) == 1


def test_parser_survives_prose_and_fences(tmp_path):
    store = _store(tmp_path)
    reply = (
        "Here are my proposals:\n```json\n" + _GOOD_REPLY + "\n```\n"
        "Hope these help!"
    )
    out = pg.generate_source_proposals(
        store, _fake_ai(reply), existing_coverage=_COVERAGE)
    assert out["proposed"] == ["or_ccb_license"]


# ---------------------------------------------------------------------------
# sanitizer hard rules — garbage never enters the quarantine
# ---------------------------------------------------------------------------

def _rejects(tmp_path, proposal, expect):
    store = _store(tmp_path)
    pg.seed_known(store)
    reply = json.dumps([proposal])
    out = pg.generate_source_proposals(
        store, _fake_ai(reply), existing_coverage=_COVERAGE)
    assert out["proposed"] == []
    assert expect in out["rejected"][0]["reason"], out["rejected"][0]


def _base():
    import copy
    return json.loads(_GOOD_REPLY)[0]


def test_reject_missing_keys(tmp_path):
    p = _base()
    del p["phone_column"]
    _rejects(tmp_path, p, "phone_column")


def test_reject_missing_trade_column(tmp_path):
    """The verifier checks trade_values against this column — a proposal
    without it is unverifiable and must never enter the quarantine."""
    p = _base()
    del p["trade_column"]
    _rejects(tmp_path, p, "trade_column")


def test_reject_bad_source_id_shape(tmp_path):
    p = _base()
    p["source_id"] = "Oregon CCB!!"
    _rejects(tmp_path, p, "not a lowercase slug")


def test_reject_known_dead_route(tmp_path):
    p = _base()
    p["source_id"] = "cslb_portal"
    _rejects(tmp_path, p, "KNOWN route")


def test_reject_known_good_route(tmp_path):
    """Known-GOOD means we already have it — a re-proposal is pointless."""
    p = _base()
    p["source_id"] = "wa_license"
    _rejects(tmp_path, p, "KNOWN route")


def test_reject_non_soda_endpoint(tmp_path):
    p = _base()
    p["endpoint"] = "https://www.oregon.gov/ccb/pages/licenses.aspx"
    _rejects(tmp_path, p, "not an https SODA resource URL")


def test_reject_invalid_state(tmp_path):
    p = _base()
    p["state"] = "ZZ"
    _rejects(tmp_path, p, "not a US state code")


def test_reject_unknown_trade_slug(tmp_path):
    p = _base()
    p["trade_values"] = {"swimming_pools": ["Pool"]}
    _rejects(tmp_path, p, "unknown trade slug")


def test_reject_empty_trade_values(tmp_path):
    p = _base()
    p["trade_values"] = {}
    # Caught by the required-keys rule first — same rejection, earlier gate.
    _rejects(tmp_path, p, "trade_values")


def test_reject_non_list_trade_values(tmp_path):
    p = _base()
    p["trade_values"] = {"gc": "General Contractor"}
    _rejects(tmp_path, p, "non-empty strings")


# ---------------------------------------------------------------------------
# honest zeros
# ---------------------------------------------------------------------------

def test_llm_failure_is_a_reason_not_a_crash(tmp_path):
    store = _store(tmp_path)

    def dead(_prompt):
        raise RuntimeError("router down")

    out = pg.generate_source_proposals(
        store, dead, existing_coverage=_COVERAGE)
    assert out["proposed"] == []
    assert "LLM call failed" in out["reason"]


def test_garbage_reply_is_an_honest_zero(tmp_path):
    store = _store(tmp_path)
    out = pg.generate_source_proposals(
        store, _fake_ai("I could not find any datasets, sorry."),
        existing_coverage=_COVERAGE)
    assert out["proposed"] == []
    assert "no usable new source" in out["reason"]


# ---------------------------------------------------------------------------
# coverage computation
# ---------------------------------------------------------------------------

def test_promoted_scout_sources_count_as_covered(tmp_path):
    """A promoted scout source's state is live coverage — the prompt must
    respect it exactly like a hand connector's."""
    store = _store(tmp_path)
    store.propose("or_ccb_license", "soda", "OR CCB",
                  "https://data.oregon.gov/resource/x.json",
                  {"state": "OR"}, "t")
    store.mark_verified("or_ccb_license")
    store.start_probation("or_ccb_license")
    store.promote("or_ccb_license")

    covered = pg._covered_states(_COVERAGE, store)
    assert covered["OR"] == ["or_ccb_license"]
    assert covered["WA"] == ["wa_license"]
