"""P9.1 — the catalog pre-fetch contracts.

The Socrata catalog fetch is what grounds the scout: real ids, real
domains, real columns. These tests pin the filtering rules (gap states
only, a phone column required), the domain→state mapping, and the
honest per-query error handling — all against httpx MockTransport, no
network.
"""

from __future__ import annotations

import httpx

from app.source_scout import catalog as ct


def _catalog_hit(dataset_id="pzjw-h5rt", domain="data.oregon.gov",
                 name="Oregon CCB Licenses", columns=("businessname",
                                                      "phonenumber")):
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


def test_domain_state_rejects_city_and_lookalikes():
    assert ct.domain_state("data.cityofchicago.org") is None
    assert ct.domain_state("oregonexplorer.info") is None
    assert ct.domain_state("") is None
    assert ct.domain_state("api.us.socrata.com") is None


# ---------------------------------------------------------------------------
# candidate filtering
# ---------------------------------------------------------------------------

def test_gap_state_and_phone_column_filters():
    hits = [
        _catalog_hit("or-1", "data.oregon.gov", columns=("businessname",
                                                         "phonenumber")),
        # covered state (WA) — dropped even with a phone column
        _catalog_hit("wa-1", "data.wa.gov", columns=("businessname",
                                                     "phonenumber")),
        # gap state but no phone column — dropped
        _catalog_hit("co-1", "data.colorado.gov",
                     columns=("businessname", "city")),
        # city domain (no state) — dropped
        _catalog_hit("chi-1", "data.cityofchicago.org",
                     columns=("businessname", "phone")),
    ]
    out = ct.fetch_catalog_candidates(
        {"OR", "CO"}, transport=_transport({"contractor license": hits}))

    ids = [c["id"] for c in out["candidates"]]
    assert ids == ["or-1"]
    assert out["queries"][0]["kept"] == 1
    assert out["errors"] == []


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
