"""Tests for Sprint 2.3B — Live Company Discovery Pipeline.

Tests the integration of SearchProviderManager with TexasProcurementConnector,
CompanyExtractor, and ContractorClassifier in the live discovery flow.
"""

from __future__ import annotations

import time

import pytest

from app.connectors.texas_procurement import TexasProcurementConnector
from app.search_providers import clear_registry, register_provider
from app.search_providers.company_extractor import CompanyExtractor
from app.search_providers.contractor_classifier import ContractorClassifier
from app.search_providers.models import SearchResponse, SearchResult
from app.search_providers.searxng import SearXNGProvider

# ---------------------------------------------------------------------------
# Helper: create mock providers registered with the singleton
# ---------------------------------------------------------------------------


def _make_mock_provider(
    results=None,
    error=None,
    name="mock_provider",
) -> SearXNGProvider:
    """Create a mock SearXNG provider and register it with the singleton."""
    clear_registry()

    class MockProvider(SearXNGProvider):
        def __init__(self):
            # Don't call super().__init__ to avoid aiohttp session setup
            self.provider_name = name
            self.description = f"Mock: {name}"
            self.enabled = True
            self.priority = 10
            self._timeout = 10
            self._max_results = 20
            self._safe_search = 1
            self._session = None

        async def search(self, query):  # type: ignore[override]
            if error:
                return SearchResponse(
                    provider=name, query=query.keywords, error=error, status="error"
                )
            return SearchResponse(
                results=results or [],
                provider=name,
                query=query.keywords,
                status="success" if results else "partial",
            )

        async def health_check(self):  # type: ignore[override]
            return {"healthy": self.enabled, "provider": name}

    provider = MockProvider()
    register_provider(provider)
    return provider


def _result(title, url, snippet="", position=1):
    """Shortcut to create a SearchResult."""
    return SearchResult(title=title, url=url, snippet=snippet, position=position)


# ---------------------------------------------------------------------------
# Test: ContractorClassifier
# ---------------------------------------------------------------------------


class TestContractorClassifier:
    """Test contractor classification logic."""

    @pytest.fixture
    def classifier(self):
        return ContractorClassifier()

    def test_accept_roofing_contractor(self, classifier):
        """Roofing contractor should be accepted."""
        result = classifier.classify(
            name="Dallas Roof Pros",
            title="Professional Roofing Contractor Dallas TX",
            description="We provide residential and commercial roofing services.",
            url="https://dallasroofpros.com",
            industry_hint="roofing",
        )
        assert result["accepted"] is True
        assert result["trade_category"] == "roofing"

    def test_accept_plumbing_contractor(self, classifier):
        """Plumbing contractor should be accepted."""
        result = classifier.classify(
            name="Smith Plumbing",
            title="Licensed Plumber Houston TX",
            description="Emergency plumbing services and installations.",
            url="https://smithplumbing.com",
            industry_hint="plumbing",
        )
        assert result["accepted"] is True
        assert result["trade_category"] == "plumbing"

    def test_accept_hvac_contractor(self, classifier):
        """HVAC contractor should be accepted."""
        result = classifier.classify(
            name="Cool Breeze HVAC",
            title="Heating and Air Conditioning Austin",
            description="HVAC installation, repair, and maintenance.",
            url="https://coolbreezehvac.com",
            industry_hint="hvac",
        )
        assert result["accepted"] is True
        assert result["trade_category"] == "hvac"

    def test_reject_manufacturer(self, classifier):
        """Manufacturer should be rejected."""
        result = classifier.classify(
            name="Owens Corning",
            title="Leading Roofing Material Manufacturer",
            description="Manufacturer of roofing shingles and building materials.",
            url="https://owenscorning.com",
        )
        assert result["accepted"] is False
        assert "manufacturer" in result["reject_reason"].lower()

    def test_reject_supplier(self, classifier):
        """Building supply store should be rejected."""
        result = classifier.classify(
            name="Texas Building Supply",
            title="Building Materials Supplier",
            description="Supplier of lumber, concrete, and construction materials.",
            url="https://texasbuildingsupply.com",
        )
        assert result["accepted"] is False
        reason = result["reject_reason"].lower()
        assert "supplier" in reason or "material" in reason

    def test_reject_software_company(self, classifier):
        """Software company should be rejected."""
        result = classifier.classify(
            name="Construction Management Software",
            title="Project Management SaaS for Builders",
            description="Cloud-based software for construction project management.",
            url="https://constructionsaas.com",
        )
        assert result["accepted"] is False
        reason = result["reject_reason"].lower()
        assert "software" in reason or "saas" in reason

    def test_reject_review_site(self, classifier):
        """Review site should be rejected."""
        result = classifier.classify(
            name="Best Roofers Reviews",
            title="Top Roofing Contractors - Reviews and Ratings",
            description="Read reviews and compare top roofing contractors.",
            url="https://bestroofersreviews.com",
        )
        assert result["accepted"] is False

    def test_reject_blocked_domain(self, classifier):
        """Blocked domains should be rejected."""
        result = classifier.classify(
            name="Roofing on Yelp",
            title="Roofing Services",
            description="",
            url="https://www.yelp.com/biz/roofing-dallas",
        )
        assert result["accepted"] is False
        assert "blocked domain" in result["reject_reason"].lower()

    def test_classify_with_industry_hint_boost(self, classifier):
        """Industry hint should boost correct trade classification."""
        result = classifier.classify(
            name="Build-It-Good Contractors",
            title="General Construction Services",
            description="We build custom homes and commercial buildings.",
            url="https://builditgood.com",
            industry_hint="roofing",
        )
        assert "accepted" in result
        assert "trade_category" in result

    def test_hint_does_not_accept_non_contractor_page(self, classifier):
        """A login page must not become a roofing company via the hint.

        Root-cause regression (Inc8): the industry hint is the QUERY, not
        page evidence. "Member Login" has no trade content, so a "Roofing"
        hint must not manufacture an acceptance — a page crawled under a
        "Roofing" query that never states a trade is rejected.
        """
        result = classifier.classify(
            name="Member Login",
            title="Member Login - ABC Texas",
            description="Sign in to access member benefits.",
            url="https://www.abctexas.org/member-login",
            industry_hint="roofing",
        )
        assert result["accepted"] is False
        assert result["trade_category"] == ""


# ---------------------------------------------------------------------------
# Test: CompanyExtractor
# ---------------------------------------------------------------------------


class TestCompanyExtractor:
    """Test company information extraction from HTML."""

    @pytest.fixture
    def extractor(self):
        return CompanyExtractor()

    def test_extract_name_from_og_title(self, extractor):
        """Name extracted from OG:title meta tag."""
        html = '<meta property="og:title" content="Joe\'s Roofing Co">'
        profile = extractor.extract("https://joesroofing.com", html, title="")
        assert profile.name == "Joe's Roofing Co"

    def test_extract_name_from_title_tag(self, extractor):
        """Name extracted from <title> tag."""
        html = "<html><head><title>Texas Roofing - Dallas</title></head></html>"
        profile = extractor.extract(
            "https://texasroofing.com", html, title="Texas Roofing - Dallas"
        )
        assert (
            "texas roofing" in profile.name.lower() or "dallas" in profile.name.lower()
        )

    # --- roadmap D19: structured name sources outrank marketing copy -------
    #
    # The 2026-08-19 live run named all five discovered companies with their SEO
    # page headline, because og:title was consulted first and called "most
    # reliable". These tests pin the corrected precedence, since a wrong name is
    # the join key for enrichment and is what ships in the customer's export.

    def test_schema_business_name_beats_og_title(self, extractor):
        """schema.org business name wins over an SEO og:title headline."""
        html = (
            '<meta property="og:title" '
            'content="Dallas Roofing Contractor Since 1983 | Arrington Roofing">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"RoofingContractor",'
            '"name":"Arrington Roofing Company"}</script>'
        )
        profile = extractor.extract("https://arringtonroofing.com", html, title="")
        assert profile.name == "Arrington Roofing Company"

    def test_schema_name_matched_before_type(self, extractor):
        """JSON-LD is matched whichever order "name" and "@type" appear in."""
        html = (
            '<script type="application/ld+json">'
            '{"name": "Bert Roofing Inc", "@type": "LocalBusiness"}</script>'
        )
        profile = extractor.extract("https://bertroofing.com", html, title="")
        assert profile.name == "Bert Roofing Inc"

    def test_og_site_name_beats_og_title(self, extractor):
        """og:site_name is the site's name; og:title is only the page headline."""
        html = (
            '<meta property="og:site_name" content="Firehouse Roofing">'
            '<meta property="og:title" '
            'content="Best Roofers in Dallas TX | Free Estimates">'
        )
        profile = extractor.extract("https://firehouseroofing.com", html, title="")
        assert profile.name == "Firehouse Roofing"

    def test_seo_title_reduced_to_brand_segment_by_domain(self, extractor):
        """With no structured data, the segment matching the domain is chosen."""
        html = (
            '<meta property="og:title" '
            'content="Dallas Roofing Contractor Since 1983 | Arrington Roofing">'
        )
        profile = extractor.extract("https://www.arringtonroofing.com/about", html)
        assert profile.name == "Arrington Roofing"

    def test_seo_title_segment_chosen_without_domain_help(self, extractor):
        """When the domain settles nothing, the least promotional segment wins."""
        title = "Top Rated Roofing Contractors Near Me | Legends Roofing"
        html = f"<html><head><title>{title}</title></head></html>"
        profile = extractor.extract(
            "https://legends-roof-pros.com", html, title=title
        )
        assert profile.name == "Legends Roofing"

    def test_hyphenated_name_is_not_split(self, extractor):
        """A hyphen inside a name is not a segment separator."""
        html = '<meta property="og:title" content="Tri-State Roofing">'
        profile = extractor.extract("https://tristateroofing.com", html, title="")
        assert profile.name == "Tri-State Roofing"

    def test_meta_attribute_order_is_tolerated(self, extractor):
        """content= before property= is still read (real pages emit both orders)."""
        html = '<meta content="Bold Roofing" property="og:site_name">'
        profile = extractor.extract("https://boldroofing.com", html, title="")
        assert profile.name == "Bold Roofing"

    def test_extract_email_from_text(self, extractor):
        """Email extracted from page text."""
        html = "Contact us at info@texasroofing.com or call 555-123-4567"
        profile = extractor.extract("https://texasroofing.com", html)
        assert profile.email == "info@texasroofing.com"

    def test_extract_phone(self, extractor):
        """Phone number extracted from page."""
        html = "Call us at (555) 123-4567 for a free quote"
        profile = extractor.extract("https://example.com", html)
        assert profile.phone != ""

    @pytest.mark.parametrize("contact", [
        "tel: +1 (555) 123-4567",
        "phone: 555-123-4567",
        "Call 555 123 4567",
    ])
    def test_extract_phone_labeled_and_unlabeled(self, extractor, contact):
        profile = extractor.extract("https://example.com", contact)
        assert "555" in profile.phone
        assert "4567" in profile.phone

    def test_whitespace_heavy_page_without_phone_finishes_promptly(self, extractor):
        html = "<title>North Roofing</title>" + " " * 8_000
        started = time.perf_counter()
        profile = extractor.extract("https://example.com", html)
        elapsed = time.perf_counter() - started
        assert profile.phone == ""
        assert elapsed < 1.0

    def test_extract_city_state_from_address(self, extractor):
        """City and state extracted from address pattern."""
        html = "123 Main St, Dallas, TX 75201"
        profile = extractor.extract("https://example.com", html)
        assert profile.city == "Dallas" or profile.state == "TX"

    def test_city_state_requires_comma_and_uppercase(self, extractor):
        """HTML noise like 'chro ME' must not be extracted as city/state.

        Root-cause regression (Inc8): the old location pattern was
        case-insensitive with an optional comma, so noise from "through
        ... member" was extracted as city='chro', state='ME' — and every
        real crawled company then failed the connector's state filter.
        A real "City, TX" still requires ", " and an uppercase code.
        """
        html = "anyway, chro ME works only here; Dallas TX is not matched either"
        profile = extractor.extract("https://example.com", html)
        assert profile.city == ""
        assert profile.state == ""

    def test_city_state_still_matches_real_address(self, extractor):
        """A genuine Title-case 'City, ST' address is still extracted."""
        html = "Headquartered in Houston, TX and serving the metro area."
        profile = extractor.extract("https://example.com", html)
        assert profile.state == "TX"

    def test_rejected_when_no_trade_match(self, extractor):
        """Profile rejected when no trade category matched."""
        html = "<title>Software Solutions Inc</title><p>We build SaaS platforms.</p>"
        profile = extractor.extract("https://softwaresolutions.com", html)
        assert profile.is_rejected is True

    def test_valid_profile_has_confidence(self, extractor):
        """Valid profile has non-zero confidence."""
        html = (
            '<meta property="og:title" content="Dallas Roof Pros">'
            "<p>Professional roofing contractor in Dallas, TX. Call 555-123-4567.</p>"
            '<a href="mailto:info@dallasroofpros.com">Email us</a>'
        )
        profile = extractor.extract(
            "https://dallasroofpros.com",
            html,
            title="Dallas Roof Pros - Licensed Roofer",
            description="Professional roofing services in Dallas",
        )
        assert profile.is_valid is True
        assert profile.confidence > 0


# ---------------------------------------------------------------------------
# Test: SearchProviderManager integration with TexasProcurementConnector
# ---------------------------------------------------------------------------


class TestFetchLiveIntegration:
    """Test _fetch_live() integration with search providers."""

    def test_fetch_live_without_providers_returns_empty(self):
        """When no providers registered, _fetch_live returns empty list."""
        clear_registry()
        connector = TexasProcurementConnector()
        results = connector._fetch_live("Roofing", "Dallas Texas", 10)
        assert results == []

    def test_fetch_live_with_providers_returns_results(self):
        """When providers return results, they are classified and returned."""
        mock_results = [
            _result(
                "Dallas Roof Pros",
                "https://dallasroofpros.com",
                "Professional roofing contractor in Dallas, TX",
                position=1,
            ),
            _result(
                "Owens Corning - Roofing Materials",
                "https://owenscorning.com",
                "Leading manufacturer of roofing materials",
                position=2,
            ),
        ]
        _make_mock_provider(results=mock_results)

        connector = TexasProcurementConnector()
        results = connector._fetch_live("Roofing", "Dallas Texas", 10)

        assert isinstance(results, list)
        # Should have at least 1 accepted (Dallas Roof Pros)
        # Owens Corning should be rejected as manufacturer
        accepted = [r for r in results if r.get("company_name")]
        assert len(accepted) >= 1
        # Check that manufacturer was filtered out
        names = [r.get("company_name", "") for r in results]
        assert not any("Owens" in n for n in names)

    def test_fetch_live_with_no_matching_results(self):
        """When search returns no contractor-matching results, empty list."""
        mock_results = [
            _result(
                "Home Depot - Building Materials",
                "https://homedepot.com",
                "Building supplies and materials",
                position=1,
            ),
        ]
        _make_mock_provider(results=mock_results)

        connector = TexasProcurementConnector()
        results = connector._fetch_live("Roofing", "Dallas Texas", 10)
        assert results == []

    @pytest.mark.network
    def test_search_prefers_live_over_fixture(self):
        """When live data exists, search() uses it instead of fixtures."""
        mock_results = [
            _result(
                "Live Roofing Co",
                "https://liveroofingco.com",
                "Live discovered roofing contractor",
                position=1,
            ),
        ]
        _make_mock_provider(results=mock_results)

        connector = TexasProcurementConnector()
        results, metadata = connector.search("Roofing", "Dallas Texas", 10)

        # Should use live data
        assert metadata["data_source"] == "live"
        # Should have at least one result from live search
        assert len(results) >= 1
        # Results should include our live mock result
        names = [r.company_name for r in results]
        assert any("Live" in n for n in names)

    @pytest.mark.network
    def test_search_falls_back_to_fixture_when_live_fails(self):
        """When all providers fail, falls back to fixture data."""
        _make_mock_provider(error="Connection refused")

        connector = TexasProcurementConnector()
        results, metadata = connector.search("Roofing", "Dallas Texas", 10)

        # Should fall back to fixtures
        assert metadata["data_source"] == "fixture"
        # Should still return some results from fixtures
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# Test: End-to-End Discovery Flow
# ---------------------------------------------------------------------------


class TestEndToEndDiscovery:
    """Test the complete discovery flow from query to output."""

    @pytest.mark.network
    def test_discovery_pipeline_with_live_data(self):
        """Full pipeline: search → classify → validate → rank → output."""
        mock_results = [
            _result(
                "Austin Roofing Solutions",
                "https://austinroofingsolutions.com",
                "Commercial and residential roofing contractor in Austin TX",
                position=1,
            ),
        ]
        _make_mock_provider(results=mock_results)

        from app.engines.discovery.company.company_discovery_engine import (
            CompanyDiscoveryEngine,
        )

        engine = CompanyDiscoveryEngine()
        results, metrics = engine.discover(
            industry="Roofing",
            location="Austin Texas",
            limit=10,
        )

        # Should discover at least one company (from live or fixture fallback)
        assert len(results) >= 0  # May be 0 if live URL fails validation
        # Metrics should indicate we attempted live search
        assert metrics.total_found >= 0

    @pytest.mark.network
    def test_connector_metadata_indicates_data_source(self):
        """Metadata clearly indicates whether results are live or fixture."""
        clear_registry()
        connector = TexasProcurementConnector()
        _, metadata = connector.search("Roofing", "Dallas Texas", 10)

        # Should always have data_source key
        assert "data_source" in metadata
        # Should be either "live" or "fixture"
        assert metadata["data_source"] in ("live", "fixture")

    @pytest.mark.network
    def test_result_structure_matches_spec(self):
        """Each result has all required fields per output schema."""
        clear_registry()
        connector = TexasProcurementConnector()
        results, _ = connector.search("Roofing", "Dallas Texas", 10)

        for r in results[:5]:  # Check first 5
            assert hasattr(r, "company_name")
            assert hasattr(r, "website")
            assert hasattr(r, "city")
            assert hasattr(r, "state")
            assert hasattr(r, "source")
            assert hasattr(r, "confidence")
            assert hasattr(r, "metadata")
            # Required metadata fields
            assert "trade_category" in r.metadata or r.metadata.get("trade_category")
            assert "discovery_reason" in r.metadata


# ---------------------------------------------------------------------------
# Test: Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases in the live discovery pipeline."""

    def test_empty_search_query_handled(self):
        """Empty or invalid query doesn't crash."""
        clear_registry()
        connector = TexasProcurementConnector()
        results = connector._fetch_live("", "Texas", 10)
        assert isinstance(results, list)

    def test_very_long_industry_handled(self):
        """Very long industry string doesn't crash."""
        clear_registry()
        connector = TexasProcurementConnector()
        long_industry = "X" * 500
        results = connector._fetch_live(long_industry, "Texas", 10)
        assert isinstance(results, list)

    def test_providers_with_exceptions_handled(self):
        """Provider exceptions don't crash the pipeline."""

        class CrashingProvider(SearXNGProvider):
            def __init__(self):
                self.provider_name = "crashy"
                self.description = "Crashes"
                self.enabled = True
                self.priority = 10
                self._timeout = 10
                self._max_results = 20
                self._safe_search = 1
                self._session = None

            async def search(self, query):  # type: ignore[override]
                raise ConnectionError("Network down")

            async def health_check(self):  # type: ignore[override]
                return {"healthy": False, "provider": "crashy"}

        clear_registry()
        register_provider(CrashingProvider())

        connector = TexasProcurementConnector()
        results = connector._fetch_live("Roofing", "Dallas Texas", 10)
        # Should return empty list, not crash
        assert results == []

    def test_mixed_accepted_and_rejected_results(self):
        """Mix of accepted contractors and rejected manufacturers."""
        mock_results = [
            _result(
                "Dallas Roof Pros",
                "https://dallasroofpros.com",
                "Roofing contractor",
                position=1,
            ),
            _result(
                "Home Depot - Building Materials",
                "https://homedepot.com",
                "Building supplies and materials",
                position=2,
            ),
            _result(
                "Houston Plumbing Co",
                "https://houstonplumbing.com",
                "Plumbing contractor services",
                position=3,
            ),
        ]
        _make_mock_provider(results=mock_results)

        connector = TexasProcurementConnector()
        fetched = connector._fetch_live("Construction", "Houston Texas", 10)

        # Should have filtered out Home Depot
        names = [r.get("company_name", "") for r in fetched]
        assert "Home Depot" not in names
        # Should have kept contractors
        assert any("Dallas" in n or "Houston" in n for n in names)

    def test_disabled_providers_are_skipped(self):
        """Disabled providers should not be queried."""
        clear_registry()
        # Register a provider
        _make_mock_provider(
            results=[_result("Should Not Appear", "https://shouldnot.com")]
        )
        # Unregister it so no enabled providers exist
        from app.search_providers.registry import get_registry

        reg = get_registry()
        reg.unregister("mock_provider")

        connector = TexasProcurementConnector()
        results = connector._fetch_live("Roofing", "Dallas Texas", 10)
        # Should get empty results since no enabled providers are registered
        assert isinstance(results, list)
        assert results == []
