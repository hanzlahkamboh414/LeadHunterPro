"""P9.1 — grounded scout proposal generation contracts.

The 2026-09-14 test-server run proved a flash LLM cannot NAME Socrata
datasets (hallucinated ids, non-SODA URLs), so generation is now
grounded: a real catalog pre-fetch, an AI that only SELECTS real
candidates and MAPS real sampled values, and sanitizers that reject any
invented id, column, or value. These tests pin that contract.
"""

from __future__ import annotations

import json

from app.source_scout import proposals as pg
from app.source_scout.store import ScoutStore


def _store(tmp_path):
    return ScoutStore(db_path=str(tmp_path / "source_scout.db"))


class _AI:
    """Scripted transport — replies queued per call; prompts recorded."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.replies:
            raise AssertionError("AI called more times than scripted")
        return self.replies.pop(0)


_COVERAGE = {"gc": {"WA": "wa_license"}, "electrical": {"TX": "tdlr_license"}}

#: One real-shaped OR candidate (the WA dataset's actual shape, renamed).
_CAND_OR = {
    "id": "pzjw-h5rt",
    "domain": "data.oregon.gov",
    "state": "OR",
    "name": "Oregon CCB Contractor Licenses",
    "description": "Oregon Construction Contractors Board license records",
    "columns": ["businessname", "phonenumber", "primaryprincipalname",
                "license_type", "license_status", "city"],
    "column_labels": {
        "businessname": "BusinessName", "phonenumber": "PhoneNumber",
        "primaryprincipalname": "PrimaryPrincipalName",
        "license_type": "LicenseType", "license_status": "LicenseStatus",
        "city": "City",
    },
}

_CAND_CO = {
    "id": "abcd-1234",
    "domain": "data.colorado.gov",
    "state": "CO",
    "name": "CO Contractor Licenses",
    "description": "Colorado licensed contractors",
    "columns": ["business_name", "phone_number", "license_type",
                "license_status"],
    "column_labels": {"business_name": "Business Name",
                      "phone_number": "Phone Number",
                      "license_type": "License Type",
                      "license_status": "License Status"},
}

_CATALOG = lambda states: {  # noqa: E731 — tiny local fake
    "candidates": [_CAND_OR, _CAND_CO],
    "queries": [{"q": "contractor license", "results": 5, "kept": 2}],
    "errors": [],
}

_SELECT_REPLY = json.dumps([
    {
        "source_id": "or_ccb_license",
        "dataset_id": "pzjw-h5rt",
        "trade_column": "license_type",
        "phone_column": "phonenumber",
        "person_column": "primaryprincipalname",
        "status_column": "license_status",
    },
])

_MAP_REPLY = json.dumps([
    {"source_id": "or_ccb_license",
     "trade_values": {"gc": ["General Contractor"]}},
])

_VALUES = ["General Contractor", "Electrical Contractor", "Plumbing",
           "Accounting"]

def _SAMPLER(endpoint, column):
    return {"values": _VALUES, "rows": 4}


def _run(store, ai, **kw):
    """generate_source_proposals with the standard fakes injected."""
    return pg.generate_source_proposals(
        store, ai,
        existing_coverage=_COVERAGE,
        catalog_fetch=kw.pop("catalog_fetch", _CATALOG),
        value_sampler=kw.pop("value_sampler", _SAMPLER),
        **kw,
    )


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

    def boom(_states):
        raise AssertionError("catalog must not be fetched under the "
                             "flood guard")

    def boom_ai(_prompt):
        raise AssertionError("AI must not be called under the flood guard")

    out = pg.generate_source_proposals(
        store, boom_ai, existing_coverage=_COVERAGE,
        catalog_fetch=boom)

    assert out["proposed"] == []
    assert "flood guard" in out["reason"]


# ---------------------------------------------------------------------------
# the grounded happy path (select from real candidates, map real values)
# ---------------------------------------------------------------------------

def test_valid_grounded_proposal_lands_in_quarantine(tmp_path):
    store = _store(tmp_path)
    pg.seed_known(store)
    ai = _AI(_SELECT_REPLY, _MAP_REPLY)

    out = _run(store, ai)

    assert out["proposed"] == ["or_ccb_license"]
    assert out["reason"] == ""
    assert out["catalog"]["fresh"] == 2
    row = store.get("or_ccb_license")
    assert row["status"] == "proposed"
    assert row["kind"] == "soda"
    # The endpoint is built by US from the catalog row, never by the AI.
    assert row["endpoint"] == \
        "https://data.oregon.gov/resource/pzjw-h5rt.json"
    assert row["payload"]["state"] == "OR"
    assert row["payload"]["phone_column"] == "phonenumber"
    assert row["payload"]["trade_column"] == "license_type"
    assert row["payload"]["trade_values"] == {"gc": ["General Contractor"]}
    # Two AI calls: SELECT then MAP.
    assert len(ai.prompts) == 2
    assert "Select up to" in ai.prompts[0]
    assert "Map REAL" in ai.prompts[1]


def test_prompt_carries_coverage_known_routes_and_trades(tmp_path):
    """The SELECT prompt must tell the AI WHERE the gaps are (covered
    states), what NOT to re-select (known routes, good and dead), the
    canonical trade vocabulary, and the real candidate columns."""
    store = _store(tmp_path)
    pg.seed_known(store)
    ai = _AI("[]")

    _run(store, ai)

    assert len(ai.prompts) == 1  # "[]" = none selected -> no MAP call
    p = ai.prompts[0]
    assert "WA" in p and "wa_license" in p          # covered states listed
    assert "TX" in p and "tdlr_license" in p
    assert "public_searxng" in p                    # dead routes listed
    assert "cslb_portal" in p
    assert "gc" in p and "finishes" in p            # canonical 14 listed
    assert "NEVER" in p and "invent a dataset id" in p  # honesty rule
    assert "pzjw-h5rt" in p and "phonenumber" in p  # real candidate listed
    assert "license_type (LicenseType)" in p        # columns with labels


def test_no_catalog_candidates_means_no_ai_call(tmp_path):
    """The guess-mode fallback is GONE: an empty catalog is an honest
    zero and the AI is never asked to invent datasets."""
    store = _store(tmp_path)

    def boom(_prompt):
        raise AssertionError("AI must not be called without real "
                             "candidates")

    out = pg.generate_source_proposals(
        store, boom, existing_coverage=_COVERAGE,
        catalog_fetch=lambda states: {"candidates": [], "queries": [],
                                      "errors": ["q='x': ConnectError"]})

    assert out["proposed"] == []
    assert "never asked to guess" in out["reason"]
    assert out["catalog"]["errors"] == ["q='x': ConnectError"]


def test_already_used_dataset_is_not_offered_twice(tmp_path):
    """A candidate whose dataset id already serves (or sits in the
    store) is filtered before the AI ever sees it — when nothing fresh
    remains, the AI is not called at all."""
    store = _store(tmp_path)
    store.propose("or_first", "soda", "OR CCB",
                  "https://data.oregon.gov/resource/pzjw-h5rt.json",
                  {"state": "OR"}, "t")

    def boom(_prompt):
        raise AssertionError("AI must not be called when every candidate "
                             "is already in use")

    out = pg.generate_source_proposals(
        store, boom, existing_coverage=_COVERAGE,
        catalog_fetch=lambda states: {"candidates": [_CAND_OR],
                                      "queries": [], "errors": []},
        value_sampler=_SAMPLER)

    assert out["proposed"] == []
    assert "already in use" in out["reason"]
    assert out["catalog"]["fresh"] == 0


def test_retired_dataset_is_not_offered_again(tmp_path):
    """Retiring a bad proposal must not put its dataset id back into
    SELECT. ``_used_dataset_ids`` still scans ``retired``."""
    store = _store(tmp_path)
    store.propose("or_first", "soda", "OR CCB",
                  "https://data.oregon.gov/resource/pzjw-h5rt.json",
                  {"state": "OR"}, "t")
    store.retire("or_first", "three consecutive mechanical fails")

    def boom(_prompt):
        raise AssertionError("AI must not be called when the only candidate "
                             "is a retired dataset")

    out = pg.generate_source_proposals(
        store, boom, existing_coverage=_COVERAGE,
        catalog_fetch=lambda states: {"candidates": [_CAND_OR],
                                      "queries": [], "errors": []},
        value_sampler=_SAMPLER)

    assert out["proposed"] == []
    assert "already in use" in out["reason"]
    assert out["catalog"]["fresh"] == 0


def test_happy_path_skips_mapping_when_ai_selects_none(tmp_path):
    store = _store(tmp_path)
    ai = _AI("[]")

    out = _run(store, ai)

    assert out["proposed"] == []
    assert "no_candidates_qualified" in out["reason"]
    assert len(ai.prompts) == 1


# ---------------------------------------------------------------------------
# sanitizer hard rules — inventions never enter the quarantine
# ---------------------------------------------------------------------------

def _selects(tmp_path, selection, expect):
    store = _store(tmp_path)
    pg.seed_known(store)
    out = _run(store, _AI(json.dumps([selection]), _MAP_REPLY))
    assert out["proposed"] == []
    assert expect in out["rejected"][0]["reason"], out["rejected"][0]


def _base():
    return json.loads(_SELECT_REPLY)[0]


def test_reject_missing_keys(tmp_path):
    p = _base()
    del p["phone_column"]
    _selects(tmp_path, p, "phone_column")


def test_reject_invented_dataset_id(tmp_path):
    """The co_cda class of failure — a hallucinated id must be rejected
    with an explicit 'never invent' reason."""
    p = _base()
    p["dataset_id"] = "4xk7-ygij"
    _selects(tmp_path, p, "never invent a dataset id")


def test_reject_invented_column(tmp_path):
    p = _base()
    p["phone_column"] = "contact_phone"
    _selects(tmp_path, p, "never invent column names")


def test_reject_invented_status_column(tmp_path):
    p = _base()
    p["status_column"] = "expiry_flag"
    _selects(tmp_path, p, "not a real column")


def test_reject_bad_source_id_shape(tmp_path):
    p = _base()
    p["source_id"] = "Oregon CCB!!"
    _selects(tmp_path, p, "not a lowercase slug")


def test_reject_known_dead_route(tmp_path):
    p = _base()
    p["source_id"] = "cslb_portal"
    _selects(tmp_path, p, "KNOWN route")


def test_reject_known_good_route(tmp_path):
    p = _base()
    p["source_id"] = "wa_license"
    _selects(tmp_path, p, "KNOWN route")


def test_duplicate_proposal_is_rejected_not_duplicated(tmp_path):
    store = _store(tmp_path)
    out1 = _run(store, _AI(_SELECT_REPLY, _MAP_REPLY))
    # Round 2: the OR dataset now sits in the store, so only CO is
    # fresh — the AI is asked about CO, not the dataset it already
    # proposed, and the quarantine never gains a second row.
    out2 = _run(store, _AI("[]"))

    assert out1["proposed"] == ["or_ccb_license"]
    assert out2["proposed"] == []
    assert out2["catalog"]["fresh"] == 1
    assert len(store.list_quarantine()) == 1


def test_reject_invented_trade_value(tmp_path):
    """The MAP stage may only use values sampled from the real dataset —
    an invented value is rejected even when everything else is valid."""
    store = _store(tmp_path)
    pg.seed_known(store)
    reply = json.dumps([
        {"source_id": "or_ccb_license",
         "trade_values": {"electrical": ["Master Sparky"]}},
    ])
    out = _run(store, _AI(_SELECT_REPLY, reply))

    assert out["proposed"] == []
    assert "invents value" in out["rejected"][0]["reason"]


def test_reject_unknown_trade_slug_in_mapping(tmp_path):
    store = _store(tmp_path)
    reply = json.dumps([
        {"source_id": "or_ccb_license",
         "trade_values": {"swimming_pools": ["General Contractor"]}},
    ])
    out = _run(store, _AI(_SELECT_REPLY, reply))
    assert out["proposed"] == []
    assert "unknown trade slug" in out["rejected"][0]["reason"]


def test_unmappable_dataset_is_an_honest_rejection(tmp_path):
    """When the AI omits a dataset from the MAP reply, that is an
    honest 'its values fit no slug' rejection — never a silent skip."""
    store = _store(tmp_path)
    out = _run(store, _AI(_SELECT_REPLY, "[]"))

    assert out["proposed"] == []
    assert "no trade mapping returned" in out["rejected"][0]["reason"]


def test_failed_value_sampling_is_an_honest_rejection(tmp_path):
    """A dataset whose trade column cannot be sampled never reaches the
    AI mapping stage."""
    store = _store(tmp_path)
    out = _run(
        store, _AI(_SELECT_REPLY),
        value_sampler=(lambda endpoint, column:
                       {"values": [], "rows": 0, "error": "ConnectError"}),
    )

    assert out["proposed"] == []
    assert "could not sample real trade values" in out["rejected"][0]["reason"]


# ---------------------------------------------------------------------------
# blank/unparseable replies — retry once, then an honest zero
# ---------------------------------------------------------------------------

def test_blank_reply_is_retried_once(tmp_path):
    store = _store(tmp_path)
    ai = _AI("", _SELECT_REPLY, _MAP_REPLY)  # blank first, good second

    out = _run(store, ai)

    assert out["proposed"] == ["or_ccb_license"]
    assert len(ai.prompts) == 3  # blank + retry + MAP


def test_garbage_reply_is_an_honest_zero(tmp_path):
    store = _store(tmp_path)
    ai = _AI("I could not find any datasets, sorry.",
             "still nothing useful")
    out = _run(store, ai)
    assert out["proposed"] == []
    assert "malformed_response_retries_exhausted" in out["reason"]
    assert "no_candidates_qualified" not in out["reason"]
    assert len(ai.prompts) == 2


def test_rejected_inventions_are_not_an_empty_selection(tmp_path):
    """A non-empty reply whose ids are all sanitized away keeps its own
    reason — neither an empty selection nor a parse failure."""
    p = _base()
    p["dataset_id"] = "4xk7-ygij"
    store = _store(tmp_path)
    pg.seed_known(store)
    out = _run(store, _AI(json.dumps([p]), _MAP_REPLY))
    assert out["proposed"] == []
    assert "every selection was rejected" in out["reason"]
    assert "no_candidates_qualified" not in out["reason"]
    assert "malformed_response_retries_exhausted" not in out["reason"]


def test_llm_failure_is_a_reason_not_a_crash(tmp_path):
    store = _store(tmp_path)

    def dead(_prompt):
        raise RuntimeError("router down")

    out = _run(store, dead)
    assert out["proposed"] == []
    assert "LLM call failed" in out["reason"]


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


# ---------------------------------------------------------------------------
# parser survival (unchanged contract, still used by both stages)
# ---------------------------------------------------------------------------

def test_parser_survives_prose_and_fences():
    reply = (
        "Here are my selections:\n```json\n" + _SELECT_REPLY + "\n```\n"
        "Hope these help!"
    )
    assert len(pg._parse_proposals(reply)) == 1
    assert pg._parse_proposals("") == []
    assert pg._parse_proposals("no json here") == []
