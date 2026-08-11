"""Tests for WebsiteEnricher (Inc9 — website location-evidence enrichment).

The enricher is the missing "Visit Websites → Extract Company
Information" link: it crawls location-less candidates' websites and fills
city/state/address/trade from the page's OWN content, so the connector's
Step-3 filter and LocationVerifier can act on REAL evidence (never the
query). These tests prove the full extract → classify → fill contract
while replacing only the network:

- ``HTTPCrawler`` is stubbed at its origin module (exactly like
  ``test_directory_crawl_source``), so nothing here touches the network;
- CompanyExtractor and ContractorClassifier are the REAL production
  classes — the page-evidence contract is exercised, not mocked.

Honesty rules pinned here (CLAUDE.md §12):
  no fabrication  — unreachable / no-address pages stay un-enriched;
  no false passes — a manufacturer / login page is rejected, never kept.
"""

from __future__ import annotations

import pytest

from app.crawlers.response import CrawlResponse
from app.discovery.website.enricher import MAX_PAGES, WebsiteEnricher
from app.engines.verification.location_verifier import LocationVerifier

# ---------------------------------------------------------------------------
# Stub network layer
# ---------------------------------------------------------------------------

_STUB_STATE: dict = {
    "pages": {},
    "fail_urls": set(),
    "boom_urls": set(),
    "boom_on_enter": None,
}


def _ok_response(url: str, html: str) -> CrawlResponse:
    return CrawlResponse(
        url=url,
        status_code=200,
        content=html.encode("utf-8"),
        headers={},
        successful=True,
        robots_compliant=True,
    )


def _failed_response(url: str) -> CrawlResponse:
    return CrawlResponse(
        url=url,
        status_code=0,
        content=b"",
        headers={},
        successful=False,
        robots_compliant=True,
        error="simulated dns failure",
    )


class _StubCrawler:
    def __init__(self, config=None):
        self.config = config

    async def __aenter__(self):
        if _STUB_STATE["boom_on_enter"] is not None:
            raise _STUB_STATE["boom_on_enter"]
        return self

    async def __aexit__(self, *exc):
        pass

    async def crawl(self, request):
        url = request.url
        if url in _STUB_STATE["boom_urls"]:
            raise RuntimeError("simulated transport failure")
        if url in _STUB_STATE["fail_urls"]:
            return _failed_response(url)
        html = _STUB_STATE["pages"].get(url)
        if html is None:
            return _failed_response(url)
        return _ok_response(url, html)


@pytest.fixture(autouse=True)
def _patch_crawler(monkeypatch):
    # Patch the ORIGIN module, not the enricher: enricher imports HTTPCrawler
    # function-locally (aiohttp import boundary), so a module attribute on
    # app.discovery.website.enricher never exists. The origin patch is picked
    # up by the function-local ``from ... import`` at call time — exactly how
    # test_no_keys_live_crawl.py stubs the same connector→enricher path.
    import app.crawlers.http_crawler as module

    _STUB_STATE["pages"] = {}
    _STUB_STATE["fail_urls"] = set()
    _STUB_STATE["boom_urls"] = set()
    _STUB_STATE["boom_on_enter"] = None
    monkeypatch.setattr(module, "HTTPCrawler", _StubCrawler)
    yield


# ---------------------------------------------------------------------------
# Fixture HTML
# ---------------------------------------------------------------------------

# Realistic roofing-company page: real name, roofing trade, and a real
# street address. The description deliberately carries NO location so the
# ONLY location evidence is the address line the enricher extracts.
LONE_STAR_HTML = """
<html><head>
<title>Lone Star Roofing LLC - Roofing Contractor in Dallas</title>
<meta property="og:title" content="Lone Star Roofing LLC">
<meta name="description" content="Commercial roofing, shingle replacement and TPO installation.">
</head>
<body><h1>Lone Star Roofing LLC</h1>
<p>Lone Star Roofing LLC provides roofing services.</p>
<p>Located at 123 Main St, Dallas, TX 75201.</p>
</body></html>
"""

# A real page with a trade but NO location mention anywhere.
NO_ADDRESS_HTML = """
<html><head>
<title>Rooftop Contractors - Roofing Services</title>
<meta property="og:title" content="Rooftop Contractors">
<meta name="description" content="Residential roofing, shingles and flat roofs.">
</head>
<body><h1>Rooftop Contractors</h1>
<p>We provide roofing services across the region.</p>
</body></html>
"""

# Manufacturer — must be rejected by the classifier gate.
OWENS_HTML = """
<html><head>
<title>Owens Corning - Roofing Material Manufacturer</title>
<meta name="description" content="Manufacturer of roofing shingles and building materials.">
</head>
<body><h1>Owens Corning</h1></body></html>
"""

# Login flow — no trade content; must be rejected even with a "Roofing"
# hint (Inc8 root-cause fix: the hint never manufactures acceptance).
LOGIN_HTML = """
<html><head>
<title>Member Login - ABC Texas</title>
<meta name="description" content="Sign in to access member benefits.">
</head>
<body><h1>Member Login</h1>
<p>Please sign in with your association account.</p>
</body></html>
"""


def _candidate(
    url: str,
    name: str = "Candidate Co",
    *,
    city: str = "",
    state: str = "",
) -> dict:
    """A search-source-style candidate: no location unless told otherwise."""
    return {
        "company_name": name,
        "website": url,
        "source_url": url,
        "city": city,
        "state": state,
        "trade_category": "",
        "industry_focus": "roofing contractor dallas tx",
        "data_provenance": "live:1",
    }


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class TestContract:
    def test_enrich_never_raises_on_empty_input(self):
        kept, stats = WebsiteEnricher().enrich([], industry="Roofing", limit=10)
        assert kept == []
        assert stats["reason"] == "no_candidates"

    def test_enrich_never_raises_on_crawler_setup_failure(self):
        _STUB_STATE["boom_on_enter"] = RuntimeError("boom")
        candidate = _candidate("https://www.example.com/")
        kept, stats = WebsiteEnricher().enrich([candidate], industry="Roofing", limit=10)
        # Candidates survive unchanged; the failure is reported, never fatal.
        assert len(kept) == 1
        assert "error" in stats


# ---------------------------------------------------------------------------
# Happy path: real page evidence fills the record
# ---------------------------------------------------------------------------


class TestEnrichment:
    def test_fills_city_state_address_from_real_page(self):
        url = "https://www.lonestarroofing.com/"
        _STUB_STATE["pages"] = {url: LONE_STAR_HTML}
        candidate = _candidate(url)
        kept, stats = WebsiteEnricher().enrich(
            [candidate], industry="Roofing", limit=10
        )

        assert len(kept) == 1
        enriched = kept[0]
        assert enriched["company_name"] == "Lone Star Roofing LLC"
        assert enriched["city"] == "Dallas"
        assert enriched["state"] == "TX"
        # CompanyExtractor reduces the page's address line to city/state only
        # (profile.address is never populated), so the reconstructed
        # "Dallas, TX" IS the clean evidence LocationVerifier VERIFIES.
        assert enriched["address"] == "Dallas, TX"
        assert enriched["trade_category"] == "roofing"
        assert "enrich:" in enriched["data_provenance"]
        assert enriched["enrichment"]["status"] == "success"
        assert stats["crawled"] == 1
        assert stats["enriched"] == 1
        assert stats["rejected"] == 0

    def test_filled_address_is_verifiable_by_location_verifier(self):
        """The address contract the connector's gate consumes (Inc9 core).

        LocationVerifier treats raw city/state as unverified CLAIMS; only
        the explicit address evidence VERIFIES. The enricher must therefore
        write an address the verifier confirms for the query target.
        """
        url = "https://www.lonestarroofing.com/"
        _STUB_STATE["pages"] = {url: LONE_STAR_HTML}
        candidate = _candidate(url)
        kept, _ = WebsiteEnricher().enrich(
            [candidate], industry="Roofing", limit=10
        )
        enriched = kept[0]

        result = LocationVerifier().verify(
            city=enriched["city"],
            state=enriched["state"],
            evidence_texts=[enriched["address"]],
            query="Dallas Texas",
        )
        assert result.location_match is True
        assert result.city == "Dallas"
        assert result.state == "TX"

    def test_page_with_no_address_stays_unlocated(self):
        """No location on the page = no location on the record (honest)."""
        url = "https://www.rooftopcontractors.com/"
        _STUB_STATE["pages"] = {url: NO_ADDRESS_HTML}
        candidate = _candidate(url)
        kept, stats = WebsiteEnricher().enrich(
            [candidate], industry="Roofing", limit=10
        )

        assert len(kept) == 1
        assert kept[0]["city"] == ""
        assert kept[0]["state"] == ""
        assert stats["no_location"] == 1
        assert stats["enriched"] == 0


# ---------------------------------------------------------------------------
# Rejection gate: garbage never becomes a company
# ---------------------------------------------------------------------------


class TestRejection:
    def test_manufacturer_page_is_dropped(self):
        url = "https://www.owenscorning.com/"
        _STUB_STATE["pages"] = {url: OWENS_HTML}
        candidate = _candidate(url, name="Owens Corning")
        kept, stats = WebsiteEnricher().enrich(
            [candidate], industry="Roofing", limit=10
        )

        assert kept == []
        assert stats["rejected"] == 1

    def test_login_page_is_dropped_even_with_roofing_hint(self):
        """Inc8 regression: the query hint must not manufacture a trade."""
        url = "https://www.abctexas.org/member-login"
        _STUB_STATE["pages"] = {url: LOGIN_HTML}
        candidate = _candidate(url, name="Member Login")
        kept, stats = WebsiteEnricher().enrich(
            [candidate], industry="Roofing", limit=10
        )

        assert kept == []
        assert stats["rejected"] == 1


# ---------------------------------------------------------------------------
# Skip / bound behavior
# ---------------------------------------------------------------------------


class TestSkipAndBound:
    def test_already_located_candidates_are_not_recrawled(self):
        candidate = _candidate(
            "https://www.already.com/", city="Dallas", state="TX"
        )
        kept, stats = WebsiteEnricher().enrich(
            [candidate], industry="Roofing", limit=10
        )

        assert len(kept) == 1
        assert stats["already_located"] == 1
        assert stats["pages_attempted"] == 0

    def test_unreachable_candidate_is_kept_unchanged(self):
        url = "https://www.deadroof.com/"
        _STUB_STATE["fail_urls"] = {url}
        candidate = _candidate(url)
        kept, stats = WebsiteEnricher().enrich(
            [candidate], industry="Roofing", limit=10
        )

        # Honest unknown: kept, un-enriched — Step-3 drops it downstream.
        assert len(kept) == 1
        assert kept[0]["state"] == ""
        assert stats["fetch_failures"] == 1

    def test_candidate_without_url_is_kept_unchanged(self):
        candidate = {
            "company_name": "No Site",
            "website": "",
            "source_url": "",
            "city": "",
            "state": "",
        }
        kept, stats = WebsiteEnricher().enrich(
            [candidate], industry="Roofing", limit=10
        )

        assert len(kept) == 1
        assert stats["no_url"] == 1
        assert stats["pages_attempted"] == 0

    def test_shared_url_is_crawled_once_for_many_candidates(self):
        url = "https://www.lonestarroofing.com/"
        _STUB_STATE["pages"] = {url: LONE_STAR_HTML}
        candidates = [_candidate(url, name=f"dup-{i}") for i in range(3)]
        kept, stats = WebsiteEnricher().enrich(
            candidates, industry="Roofing", limit=10
        )

        assert len(kept) == 3
        assert stats["pages_attempted"] == 1
        assert all(c["state"] == "TX" for c in kept)

    def test_crawl_is_bounded_by_max_pages(self):
        urls = [f"https://www.firm{i}.com/" for i in range(40)]
        _STUB_STATE["pages"] = {u: LONE_STAR_HTML for u in urls}
        candidates = [_candidate(u) for u in urls]
        kept, stats = WebsiteEnricher().enrich(
            candidates, industry="Roofing", limit=100
        )

        assert stats["pages_attempted"] == MAX_PAGES
        # Every candidate survives — the ones past the cap stay un-enriched.
        assert len(kept) == 40
        assert stats["candidates_out"] == 40
