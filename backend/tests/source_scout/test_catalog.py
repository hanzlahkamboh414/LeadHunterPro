"""P9.1 — the catalog pre-fetch contracts.

The Socrata catalog fetch is what grounds the scout: real ids, real
domains, real columns. These tests pin the filtering rules (a gap state
plus the three columns the SELECT qualify bar needs — phone, person,
trade/license type), the domain→state mapping, and the honest per-query
error handling — all against httpx MockTransport, no network.
"""

from __future__ import annotations

import httpx

from app.source_scout import catalog as ct


def _catalog_hit(dataset_id="pzjw-h5rt", domain="data.oregon.gov",
                 name="Oregon CCB Licenses", columns=("businessname",
                                                      "phonenumber",
                                                      "primaryprincipalname",
                                                      "license_type")):
    return {
        "resource": {
            "id": dataset_id,
            "name": name,
            "description": "Contractor licenses",
            "columns_field_name": list(columns),
            "columns_name": [c.title() for c in columns],
        },
        "metadata": {"domain": domain},
    }


def _transport(responses):
    """Responses keyed by the catalog q param (single canned list each)."""
    def handler(request):
        q = dict(request.url.params).get("q", "")
        if q in responses:
            return httpx.Response(200, json={"results": responses[q]})
        return httpx.Response(200, json={"results": []})
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# domain -> state
# ---------------------------------------------------------------------------

def test_domain_state_full_name_and_abbr():
    assert ct.domain_state("data.oregon.gov") == "OR"
    assert ct.domain_state("data.ny.gov") == "NY"
    assert ct.domain_state("DATA.COLORADO.GOV") == "CO"


def test_domain_state_rejects_lookalikes_and_unattributable_hosts():
    assert ct.domain_state("oregonexplorer.info") is None
    assert ct.domain_state("") is None
    assert ct.domain_state("api.us.socrata.com") is None


def test_domain_state_municipal_portals_map_to_their_state():
    """A city host names a CITY, not a state — the state filter used to
    drop the biggest municipal license boards outright. Every entry is a
    real portal from the harvested universe."""
    assert ct.domain_state("data.cityofchicago.org") == "IL"
    assert ct.domain_state("datacatalog.cookcountyil.gov") == "IL"
    assert ct.domain_state("data.cityofnewyork.us") == "NY"
    assert ct.domain_state("data.lacity.org") == "CA"
    assert ct.domain_state("datahub.austintexas.gov") == "TX"
    assert ct.domain_state("cos-data.seattle.gov") == "WA"
    assert ct.domain_state("data.nola.gov") == "LA"
    assert ct.domain_state("citydata.mesaaz.gov") == "AZ"
    # hyphenated hosts are the reason the match runs on tokens, not labels
    assert ct.domain_state("internal-sandiegocounty.data.socrata.com") == "CA"
    # a foreign municipal portal stays unattributable
    assert ct.domain_state("data.edmonton.ca") is None
    assert ct.domain_state("data.calgary.ca") is None


def test_domain_state_foreign_tld_is_not_a_usps_state():
    """ccTLDs collide with USPS codes. Canada ``.ca`` is not California,
    Colombia ``.co`` is not Colorado, and ``*.gov.au`` is not US ``.gov``.
    A buried ``ca.gov`` label inside a ``.com`` host is not California
    either. ``data.ca.gov`` stays California.
    """
    assert ct.domain_state("data.winnipeg.ca") is None
    assert ct.domain_state("datos.gov.co") is None
    assert ct.domain_state("data.wa.gov.au") is None
    assert ct.domain_state("data.ca.gov.example.com") is None
    assert ct.domain_state("data.ca.gov") == "CA"
    assert ct.domain_state("data.ct.gov") == "CT"
    assert ct.domain_state("data.ny.us") == "NY"


def test_domain_state_multiword_name_is_reachable_squashed():
    """A domain label cannot hold a space, so the squashed spelling is
    the only one a portal can use."""
    assert ct.domain_state("data.newjersey.gov") == "NJ"
    assert ct.domain_state("data.newyork.gov") == "NY"
    assert ct.domain_state("data.northcarolina.gov") == "NC"
    assert ct.domain_state("data.texas.gov") == "TX"


# ---------------------------------------------------------------------------
# candidate filtering
# ---------------------------------------------------------------------------

#: The real license-board shape (WA/TX, verified live) — the bar a
#: candidate must clear to be shown to the AI.
_BOARD = ("businessname", "phonenumber", "primaryprincipalname",
          "license_type")


def test_qualify_shape_and_gap_state_filters():
    hits = [
        # a real license-board shape in a gap state — kept
        _catalog_hit("or-1", "data.oregon.gov", columns=_BOARD),
        # covered state (WA) — dropped even with a usable shape
        _catalog_hit("wa-1", "data.wa.gov", columns=_BOARD),
        # gap state, phone only (no person column) — dropped
        _catalog_hit("co-1", "data.colorado.gov",
                     columns=("businessname", "phonenumber")),
        # gap state, phone + person but no trade column — dropped
        _catalog_hit("co-2", "data.colorado.gov",
                     columns=("businessname", "phonenumber",
                              "primaryprincipalname")),
        # city portal — now attributable to IL, which is not a gap here
        _catalog_hit("chi-1", "data.cityofchicago.org", columns=_BOARD),
    ]
    out = ct.fetch_catalog_candidates(
        {"OR", "CO"}, transport=_transport({"contractor license": hits}))

    ids = [c["id"] for c in out["candidates"]]
    assert ids == ["or-1"]
    q = out["queries"][0]
    assert q["kept"] == 1
    assert q["skipped"] == {"no person column": 1,
                            "no trade/license-type column": 1}
    assert out["errors"] == []


def test_real_junk_shapes_never_reach_the_prompt():
    """The live rejects all carry a phone column, so the phone gate alone
    never stopped them: CT continuing-education COURSES (``427s-5uzd``,
    ``7quf-krzf``), DE certified asbestos VENDORS (``f677-ahd9``, the
    firm sits in ``name``), NJ inspection FACILITIES (``9qpt-bnm4``) and
    NY apparel registration (``xtgv-xf66``). The real WA/TX board shapes
    still pass."""
    junk = [
        _catalog_hit("ce-1", "data.ct.gov",
                     columns=("course_name", "phone", "company",
                              "license_types_covered", "classroom_hours")),
        _catalog_hit("de-1", "data.delaware.gov",
                     columns=("name", "phone1", "certtype", "certno")),
        _catalog_hit("nj-1", "data.nj.gov",
                     columns=("facility_name", "phone_number",
                              "inspection_type", "facility_license_id")),
        _catalog_hit("ny-1", "data.ny.gov",
                     columns=("business_name", "phone",
                              "certificate_number")),
    ]
    good = [
        # the real TDLR shape
        _catalog_hit("tx-ok", "data.texas.gov",
                     columns=("business_telephone", "owner_telephone",
                              "owner_name", "license_type",
                              "license_subtype")),
        _catalog_hit("or-ok", "data.oregon.gov", columns=_BOARD),
    ]
    out = ct.fetch_catalog_candidates(
        {"CT", "DE", "NJ", "NY", "TX", "OR"},
        transport=_transport({"contractor license": junk + good}))

    assert [c["id"] for c in out["candidates"]] == ["or-ok", "tx-ok"]
    assert out["queries"][0]["skipped"] == {"no person column": 4}


def test_personless_license_dataset_is_dropped():
    """Genuine license data that cannot serve the product: DE water-well
    contractors (``orgname``), NY mold contractors and NY elevator
    contractors carry a phone and a trade but no person column. The AI
    may only name columns that exist, so no legitimate selection is
    possible — keeping them would only consume candidate slots."""
    hits = [
        _catalog_hit("de-water", "data.delaware.gov",
                     columns=("orgname", "workphone", "licensesubtype",
                              "licstatus", "licenseexpires")),
        _catalog_hit("ny-mold", "data.ny.gov",
                     columns=("business_name", "dba_name", "phone",
                              "license_type", "license_status")),
        _catalog_hit("ny-lift", "data.ny.gov",
                     columns=("business_name", "dba_name", "phone",
                              "license_type")),
    ]
    out = ct.fetch_catalog_candidates(
        {"DE", "NY"}, transport=_transport({"contractor license": hits}))

    assert out["candidates"] == []
    assert out["queries"][0]["skipped"] == {"no person column": 3}


def test_company_name_columns_are_not_person_columns():
    """A firm-name column must never satisfy the person gate."""
    hit = _catalog_hit(
        "co-firm", "data.colorado.gov",
        columns=("businessname", "dba_name", "orgname", "company",
                 "facility_name", "employer_name", "phonenumber",
                 "license_type"))
    out = ct.fetch_catalog_candidates(
        {"CO"}, transport=_transport({"contractor license": [hit]}))

    assert out["candidates"] == []
    assert out["queries"][0]["skipped"] == {"no person column": 1}


def test_an_email_column_can_never_be_the_person_column():
    """``contact_email`` must not stand in for a person name (the
    junk-email class the phones lane already fights), while a real
    ``contact_name`` still counts."""
    email_only = _catalog_hit(
        "co-mail", "data.colorado.gov",
        columns=("contact_email", "phonenumber", "license_type"))
    real_name = _catalog_hit(
        "co-name", "data.colorado.gov",
        columns=("contact_name", "phonenumber", "license_type"))
    out = ct.fetch_catalog_candidates(
        {"CO"}, transport=_transport({"contractor license": [email_only,
                                                            real_name]}))

    assert [c["id"] for c in out["candidates"]] == ["co-name"]
    assert out["queries"][0]["skipped"] == {"no person column": 1}


def test_a_phone_column_can_never_be_the_person_column():
    """``owner_telephone`` reads like "owner" but it is a number."""
    hit = _catalog_hit("co-tel", "data.colorado.gov",
                       columns=("business_telephone", "owner_telephone",
                                "license_type"))
    out = ct.fetch_catalog_candidates(
        {"CO"}, transport=_transport({"contractor license": [hit]}))

    assert out["candidates"] == []
    assert out["queries"][0]["skipped"] == {"no person column": 1}


def test_a_dba_or_a_traded_flag_is_not_a_trade_column():
    """``trade_name`` is a business alias and ``publicly_traded`` is a
    flag — neither can carry the trade values the MAP stage needs. The
    live NY Contractor Registry (``i4jv-zkey``) carries both."""
    hit = _catalog_hit(
        "ny-reg", "data.ny.gov",
        columns=("business_name", "dba_name", "business_officers", "phone",
                 "business_is_publicly_traded", "trade_name",
                 "certificate_number"))
    out = ct.fetch_catalog_candidates(
        {"NY"}, transport=_transport({"contractor license": [hit]}))

    assert out["candidates"] == []
    assert out["queries"][0]["skipped"] == {
        "no trade/license-type column": 1}


def test_duplicate_dataset_ids_collapse():
    hit = _catalog_hit("or-1", "data.oregon.gov")
    out = ct.fetch_catalog_candidates(
        {"OR"}, transport=_transport({
            "contractor license": [hit],
            "licensed contractors": [hit],  # same dataset, second term
        }))

    assert [c["id"] for c in out["candidates"]] == ["or-1"]


def test_query_failure_is_an_honest_error_not_a_crash():
    def handler(request):
        raise httpx.ConnectError("boom")
    out = ct.fetch_catalog_candidates(
        {"OR"}, transport=httpx.MockTransport(handler))

    assert out["candidates"] == []
    assert len(out["errors"]) == len(ct.SEARCH_TERMS)
    assert all("ConnectError" in e for e in out["errors"])


def test_build_endpoint_comes_from_the_catalog_row():
    cand = _catalog_hit()
    parsed = ct._candidate(cand)
    assert ct.build_endpoint(parsed) == \
        "https://data.oregon.gov/resource/pzjw-h5rt.json"


# ---------------------------------------------------------------------------
# real value sampling
# ---------------------------------------------------------------------------

def test_sample_column_values_distinct_and_sorted():
    rows = [{"license_type": "Plumbing"},
            {"license_type": "General Contractor"},
            {"license_type": "Plumbing"},
            {"license_type": ""},
            {"license_type": None},
            {"other": "x"}]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=rows))
    out = ct.sample_column_values(
        "https://data.oregon.gov/resource/pzjw-h5rt.json", "license_type",
        transport=transport)

    assert out == {"values": ["General Contractor", "Plumbing"], "rows": 6}


def test_sample_column_values_error_is_honest():
    def handler(request):
        raise httpx.ConnectError("down")
    out = ct.sample_column_values(
        "https://data.oregon.gov/resource/pzjw-h5rt.json", "license_type",
        transport=httpx.MockTransport(handler))

    assert out["values"] == []
    assert "ConnectError" in out["error"]


def test_sample_column_values_non_list_response():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"error": True}))
    out = ct.sample_column_values(
        "https://data.oregon.gov/resource/pzjw-h5rt.json", "license_type",
        transport=transport)

    assert out["values"] == []
    assert "non-list" in out["error"]


# ---------------------------------------------------------------------------
# live sanity (skipped unless the catalog is reachable) — NOT a CI contract
# ---------------------------------------------------------------------------

def test_live_catalog_reaches_the_real_api():
    """One real request: our own WA dataset must be findable by search —
    the grounding promise depends on this API existing and shaping like
    this. Skip (not fail) when the network is unavailable."""
    import pytest

    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(
                ct.CATALOG_URL,
                params={"q": "contractor license", "only": "dataset",
                        "limit": 10},
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
    except Exception:
        pytest.skip("catalog API unreachable from here")

    wa = [h for h in results
          if (h.get("resource") or {}).get("id") == "m8qx-ubtq"]
    assert wa, "the live catalog no longer returns the WA dataset"
    cand = ct._candidate(wa[0])
    assert cand["state"] == "WA"
    assert "phonenumber" in cand["columns"]
