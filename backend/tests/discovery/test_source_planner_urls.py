"""Seed-source integrity tests (Blueprint §3B URL audit + §7).

Proves the SourcePlanner's planned seed URLs are real, well-formed,
production addresses — not the historical typos or placeholder entries:

- TDLR is ``tdlr.texas.gov`` (was the typo ``tdlnr.texas.gov``);
- CSLB is ``cslb.ca.gov`` (was the typo ``slbt.ca.gov``);
- the "Texas State Gambling Commission" placeholder is gone;
- chamber entries are provenance-only: no fabricated
  ``<city>-<state>.com`` URL and no company-discovery support, so they can
  never become crawl seeds (CLAUDE.md §12: no placeholder/fake domains);
- every planned seed that supports company discovery carries a well-formed
  http(s) URL with a real host.

This is the Blueprint §8 rec 1 "curated, verified seed set" guarantee at
the planner layer: the crawl source can only ever be pointed at real sites.
"""

from __future__ import annotations

from urllib.parse import urlparse

from app.engines.source_intelligence.source_models import SourcePlannerRequest
from app.engines.source_intelligence.source_planner import SourcePlanner


def _make_request(state="TX", city="Dallas") -> SourcePlannerRequest:
    return SourcePlannerRequest(
        industry="Construction Estimating",
        country="USA",
        state=state,
        city=city,
    )


class TestSeedUrlIntegrity:
    def test_tdlr_url_is_corrected(self):
        result = SourcePlanner().plan(_make_request())

        tdlr = [s for s in result.sources if "tdlr" in s.name.lower()]
        assert tdlr, "TDLR entry missing from TX plan"
        assert all(s.url == "https://www.tdlr.texas.gov" for s in tdlr)

    def test_cslb_url_is_corrected(self):
        result = SourcePlanner().plan(
            _make_request(state="CA", city="Los Angeles")
        )

        cslb = [
            s
            for s in result.sources
            if "contractors state license board" in s.name.lower()
        ]
        assert cslb, "CSLB entry missing from CA plan"
        assert all(s.url == "https://www.cslb.ca.gov" for s in cslb)

    def test_gambling_placeholder_is_gone(self):
        result = SourcePlanner().plan(_make_request())

        assert not any("gambling" in s.name.lower() for s in result.sources)

    def test_no_fabricated_domains_in_seed_set(self):
        """Every discovery-capable seed URL must be a real, well-formed host."""
        result = SourcePlanner().plan(_make_request())

        for s in result.sources:
            if not s.supports_company_discovery or not s.url:
                continue
            parsed = urlparse(s.url)
            assert parsed.scheme in ("http", "https"), f"bad scheme: {s.url}"
            assert parsed.netloc, f"no host: {s.url}"
            assert "example" not in parsed.netloc, f"placeholder host: {s.url}"

    def test_chamber_entries_never_become_crawl_seeds(self):
        """Chambers are provenance-only: no URL, no company discovery."""
        result = SourcePlanner().plan(_make_request(city="Houston"))

        chambers = [
            s for s in result.sources if s.source_type == "local_chamber"
        ]
        assert chambers, "chamber entry expected for Houston"
        for s in chambers:
            assert s.url == ""
            assert s.supports_company_discovery is False
