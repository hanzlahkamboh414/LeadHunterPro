"""adapter_writer tests — the AI contract gate (coverage_engine_v2.md §5).

The fake AI is just a callable returning a string; what is under test is
the HARD validation between that string and the registry: invented
columns, numbers in capabilities, non-canonical trade slugs and unknown
capability keys must all be rejected, and a rejection must leave the
source in probing with nothing written.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from app.discovery.tradefold import CANONICAL_TRADES
from app.source_scout.adapter_writer import (
    MAX_ATTEMPTS,
    AdapterError,
    adapter_of,
    build_prompt,
    parse_reply,
    validate_contract,
    write_adapter,
)
from app.source_scout.store import (
    STATUS_ADAPTER_DRAFT,
    STATUS_PROBING,
    ScoutStore,
)

COLUMNS = ["BusinessName", "BusinessPhone", "City", "State", "Classification",
           "LicenseNo"]

GOOD = {
    "fetch": {"method": "GET", "url": "https://www.cslb.ca.gov/master.xlsx",
              "format": "xlsx", "refresh": "monthly",
              "file_shape": {"header_row": 1}},
    "field_map": {"company_name": "businessname", "phone": "BusinessPhone",
                  "city": "City", "state_field": "State",
                  "license_no": "LicenseNo", "trade": "Classification"},
    "capabilities": {"phone": {"present": True}, "city": {"present": True},
                     "email": {"present": False}},
    "trade_mapping": {"B": "gc", "C-10": "electrical"},
    "estimated_rows": 280000,
}

CSLB_KNOWN = {"B": "gc", "B-2": "gc", "C-10": "electrical"}


def _store(tmp_path) -> ScoutStore:
    return ScoutStore(db_path=str(tmp_path / "scout.db"))


def _seed(store: ScoutStore, source_id="state_ca_board") -> None:
    store.seed_upsert(source_id, seed_domain="phones", state="CA", name="CSLB",
                      base_url="https://www.cslb.ca.gov",
                      seed_meta={"trade_scope": "all_trades", "priority_rank": 1})
    store.start_probing(source_id)


def _ask(reply: object, calls: list[str] | None = None):
    def ask(prompt: str) -> str:
        if calls is not None:
            calls.append(prompt)
        return reply if isinstance(reply, str) else json.dumps(reply)
    return ask


def _contract(**over) -> dict:
    out = json.loads(json.dumps(GOOD))
    out.update(over)
    return out


# ---------------------------------------------------------------------------
# parse_reply
# ---------------------------------------------------------------------------

def test_parse_reply_tolerates_fences_and_prose():
    assert parse_reply('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_reply('Here it is: {"a": 1} — done') == {"a": 1}
    with pytest.raises(AdapterError, match="no JSON object"):
        parse_reply("I cannot help with that.")
    with pytest.raises(AdapterError, match="not valid JSON"):
        parse_reply("{a: 1,}")
    with pytest.raises(AdapterError, match="not an object"):
        parse_reply("[1, 2]")


# ---------------------------------------------------------------------------
# hard validation — the anti-hallucination gates
# ---------------------------------------------------------------------------

def test_good_contract_passes_and_normalizes_column_case():
    got = validate_contract(_contract(), columns=COLUMNS,
                            access_path="bulk_file")
    assert got["field_map"]["company_name"] == "BusinessName"  # real spelling
    assert got["access_path"] == "bulk_file"
    assert got["fetch"]["method"] == "GET"


def test_invented_column_is_rejected():
    with pytest.raises(AdapterError, match="invented column"):
        validate_contract(
            _contract(field_map={"company_name": "Business_Name",
                                 "state_field": "State"}),
            columns=COLUMNS, access_path="bulk_file")


def test_missing_required_field_is_rejected():
    with pytest.raises(AdapterError, match="must map state_field"):
        validate_contract(_contract(field_map={"company_name": "BusinessName"}),
                          columns=COLUMNS, access_path="bulk_file")


def test_non_canonical_field_key_is_rejected():
    with pytest.raises(AdapterError, match="not a canonical field"):
        validate_contract(
            _contract(field_map={**GOOD["field_map"], "revenue": "City"}),
            columns=COLUMNS, access_path="bulk_file")


def test_capability_numbers_are_rejected():
    with pytest.raises(AdapterError, match="only 'present' is allowed"):
        validate_contract(
            _contract(capabilities={"phone": {"present": True,
                                              "fill_rate": 0.99}}),
            columns=COLUMNS, access_path="bulk_file")


def test_capability_must_be_boolean_and_known():
    with pytest.raises(AdapterError, match="present must be true/false"):
        validate_contract(_contract(capabilities={"phone": {"present": "yes"}}),
                          columns=COLUMNS, access_path="bulk_file")
    with pytest.raises(AdapterError, match="unknown to the router"):
        validate_contract(_contract(capabilities={"turnover": {"present": True}}),
                          columns=COLUMNS, access_path="bulk_file")


def test_present_capability_needs_a_mapped_column():
    with pytest.raises(AdapterError, match="maps no column"):
        validate_contract(
            _contract(field_map={"company_name": "BusinessName",
                                 "state_field": "State",
                                 "phone": None},
                      capabilities={"phone": {"present": True}}),
            columns=COLUMNS, access_path="bulk_file")


def test_trade_mapping_must_use_canonical_slugs():
    with pytest.raises(AdapterError, match="not a canonical trade"):
        validate_contract(_contract(trade_mapping={"B": "general contractor"}),
                          columns=COLUMNS, access_path="bulk_file")
    assert CANONICAL_TRADES  # the vocabulary is the platform's, not a guess


def test_known_trade_mapping_cannot_be_invented_around():
    got = validate_contract(_contract(), columns=COLUMNS,
                            access_path="bulk_file",
                            known_trade_mapping=CSLB_KNOWN)
    assert got["trade_mapping"] == CSLB_KNOWN  # the full known map is stored
    # a subset is honest (a pilot targets one classification) ...
    subset = validate_contract(_contract(trade_mapping={"B": "gc"}),
                               columns=COLUMNS, access_path="bulk_file",
                               known_trade_mapping=CSLB_KNOWN)
    assert subset["trade_mapping"] == CSLB_KNOWN
    # ... but a relabeled or invented code is not
    with pytest.raises(AdapterError, match="known codes"):
        validate_contract(_contract(trade_mapping={"C-10": "plumbing"}),
                          columns=COLUMNS, access_path="bulk_file",
                          known_trade_mapping=CSLB_KNOWN)
    with pytest.raises(AdapterError, match="known codes"):
        validate_contract(_contract(trade_mapping={"C-99": "gc"}),
                          columns=COLUMNS, access_path="bulk_file",
                          known_trade_mapping=CSLB_KNOWN)


def test_fetch_gates_url_format_and_zip_inner_path():
    with pytest.raises(AdapterError, match="not an http"):
        validate_contract(_contract(fetch={**GOOD["fetch"], "url": "www.x.com"}),
                          columns=COLUMNS, access_path="bulk_file")
    with pytest.raises(AdapterError, match="unreadable"):
        validate_contract(_contract(fetch={**GOOD["fetch"], "format": "xml"}),
                          columns=COLUMNS, access_path="bulk_file")
    with pytest.raises(AdapterError, match="inner_path"):
        validate_contract(
            _contract(fetch={"method": "GET", "url": "https://x/a.zip",
                             "format": "zip/csv", "file_shape": {}}),
            columns=COLUMNS, access_path="bulk_file")


def test_post_fetch_requires_a_form_block():
    with pytest.raises(AdapterError, match="form.select_field"):
        validate_contract(
            _contract(fetch={"method": "POST", "url": "https://x/portal",
                             "format": "xlsx"}),
            columns=COLUMNS, access_path="html_form")
    ok = validate_contract(
        _contract(fetch={"method": "POST", "url": "https://x/portal",
                         "format": "xlsx",
                         "form": {"select_field": "ctl$lb", "code": "B-2",
                                  "submit_field": "ctl$btn"}}),
        columns=COLUMNS, access_path="html_form")
    assert ok["fetch"]["form"]["code"] == "B-2"


def test_estimated_rows_must_be_a_positive_int():
    for bad in (0, -5, "280k", 1.5, True):
        with pytest.raises(AdapterError, match="positive integer"):
            validate_contract(_contract(estimated_rows=bad),
                              columns=COLUMNS, access_path="bulk_file")


def test_unknown_access_path_is_rejected():
    with pytest.raises(AdapterError, match="not a prober path"):
        validate_contract(_contract(), columns=COLUMNS, access_path="soda")


# ---------------------------------------------------------------------------
# form facts — a POST spec may only use controls the page really has
# ---------------------------------------------------------------------------

CSLB_FORM = {
    "method": "POST", "url": "https://www.cslb.ca.gov/portal",
    "format": "xlsx",
    "form": {"select_field": "ctl00$MainContent$lbClassification",
             "code": "B-2",
             "submit_field": "ctl00$MainContent$btnSearch",
             "submit_value": "Download"}}

CSLB_FACTS = {
    "url": "https://www.cslb.ca.gov/portal",
    "hidden": ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"],
    "selects": {"ctl00$MainContent$lbClassification": ["B-2", "C-10"]},
    "submits": [{"name": "ctl00$MainContent$btnSearch", "value": "Download"}]}


def test_post_spec_matching_the_live_page_passes():
    got = validate_contract(_contract(fetch=CSLB_FORM), columns=COLUMNS,
                            access_path="html_form",
                            source_url="https://www.cslb.ca.gov",
                            form_facts=CSLB_FACTS)
    assert got["fetch"]["form"]["code"] == "B-2"
    assert got["fetch"]["method"] == "POST"


def test_invented_select_or_button_is_rejected():
    bad_select = json.loads(json.dumps(CSLB_FORM))
    bad_select["form"]["select_field"] = "ctl00$MainContent$ddlTrade"
    with pytest.raises(AdapterError, match="not a select on the page"):
        validate_contract(_contract(fetch=bad_select), columns=COLUMNS,
                          access_path="html_form", form_facts=CSLB_FACTS)

    bad_button = json.loads(json.dumps(CSLB_FORM))
    bad_button["form"]["submit_field"] = "ctl00$MainContent$btnGo"
    with pytest.raises(AdapterError, match="not a button on the page"):
        validate_contract(_contract(fetch=bad_button), columns=COLUMNS,
                          access_path="html_form", form_facts=CSLB_FACTS)


def test_code_must_be_a_value_the_select_accepts():
    bad = json.loads(json.dumps(CSLB_FORM))
    bad["form"]["code"] = "Z-99"
    with pytest.raises(AdapterError, match="is not a value"):
        validate_contract(_contract(fetch=bad), columns=COLUMNS,
                          access_path="html_form", form_facts=CSLB_FACTS)


def test_invented_host_is_rejected_when_the_registry_knows_the_url():
    elsewhere = _contract(fetch={**GOOD["fetch"],
                                 "url": "https://data.example.gov/x.xlsx"})
    with pytest.raises(AdapterError, match="invented host"):
        validate_contract(elsewhere, columns=COLUMNS, access_path="bulk_file",
                          source_url="https://www.cslb.ca.gov")
    ok = validate_contract(_contract(), columns=COLUMNS,
                           access_path="bulk_file",
                           source_url="https://www.cslb.ca.gov")
    assert urlparse(ok["fetch"]["url"]).netloc == "www.cslb.ca.gov"


# ---------------------------------------------------------------------------
# the writer + store wiring
# ---------------------------------------------------------------------------

def test_write_adapter_stores_contract_and_enters_adapter_draft(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    out = write_adapter(store, "state_ca_board", columns=COLUMNS,
                        sample_rows=[{"BusinessName": "Acme", "State": "CA"}],
                        access_path="bulk_file", ai_ask=_ask(GOOD),
                        state="CA")
    row = out["row"]
    assert row["status"] == STATUS_ADAPTER_DRAFT
    stored = adapter_of(store.get("state_ca_board"))
    assert stored["field_map"]["company_name"] == "BusinessName"
    assert stored["fetch_spec"]["format"] == "xlsx"
    assert stored["capabilities"] == {"phone": {"present": True},
                                      "city": {"present": True},
                                      "email": {"present": False}}
    assert "fill_rate" not in json.dumps(stored["capabilities"])
    assert "adapter written via bulk_file" in row["gate_fail_reason"]


def test_write_adapter_retries_once_with_the_rejection_reason(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    calls: list[str] = []
    replies = iter([json.dumps(_contract(field_map={"company_name": "Nope",
                                                    "state_field": "State"})),
                    json.dumps(GOOD)])
    out = write_adapter(store, "state_ca_board", columns=COLUMNS,
                        sample_rows=[{"BusinessName": "Acme"}],
                        access_path="bulk_file",
                        ai_ask=lambda p: (calls.append(p), next(replies))[1])
    assert out["row"]["status"] == STATUS_ADAPTER_DRAFT
    assert len(calls) == 2
    assert "REJECTED: field_map[company_name] names column 'Nope'" in calls[1]


def test_write_adapter_gives_up_honestly_and_writes_nothing(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    with pytest.raises(AdapterError, match="rejected after 2 attempts"):
        write_adapter(store, "state_ca_board", columns=COLUMNS,
                      sample_rows=[{"BusinessName": "Acme"}],
                      access_path="bulk_file", ai_ask=_ask("no thanks"))
    row = store.get("state_ca_board")
    assert row["status"] == STATUS_PROBING  # still visible, still probing
    assert row["field_map"] == "{}"  # nothing half-written
    assert row["status"] != STATUS_ADAPTER_DRAFT
    assert MAX_ATTEMPTS == 2


def test_write_adapter_reports_ai_call_failure_without_touching_store(tmp_path):
    store = _store(tmp_path)
    _seed(store)

    def boom(prompt):
        raise RuntimeError("429 too many requests")

    with pytest.raises(AdapterError, match="AI call failed \\(RuntimeError\\)"):
        write_adapter(store, "state_ca_board", columns=COLUMNS,
                      sample_rows=[{"BusinessName": "Acme"}],
                      access_path="bulk_file", ai_ask=boom)
    assert store.get("state_ca_board")["fetch_spec"] == "{}"


def test_write_adapter_rejects_a_source_with_no_columns(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    with pytest.raises(AdapterError, match="no columns sampled"):
        write_adapter(store, "state_ca_board", columns=[], sample_rows=[],
                      access_path="bulk_file", ai_ask=_ask(GOOD))


def test_prompt_carries_real_columns_sample_and_known_codes():
    prompt = build_prompt(source_id="state_ca_board", state="CA",
                          access_path="html_form", columns=COLUMNS,
                          sample_rows=[{"BusinessName": "Acme",
                                        "State": "CA"}],
                          known_trade_mapping=CSLB_KNOWN)
    assert "BusinessName" in prompt and '"Acme"' in prompt
    assert "html_form" in prompt
    assert "ALREADY KNOWN" in prompt and '"C-10": "electrical"' in prompt
    assert "never write a number" in prompt.lower()
