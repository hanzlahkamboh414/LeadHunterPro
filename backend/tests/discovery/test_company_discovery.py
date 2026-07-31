"""Unit tests for the company discovery engine.

Covers every stage of the pipeline and every provider failure mode:
  - no API configured → structured diagnostic
  - provider unavailable (not configured)
  - CAPTCHA detection
  - request timeout
  - provider success path
  - full pipeline integration
"""

from __future__ import annotations

import logging
from unittest.mock import patch, MagicMock

import pytest

from app.engines.discovery.company.company_cleaner import clean_companies
from app.engines.discovery.company.company_models import (
    CompanyDiscoveryResult,
    DiscoveryMetrics,
)
from app.engines.discovery.company.company_search import (
    search_companies,
    DiscoveryDiagnostic,
    BingSearchProvider,
    DuckDuckGoSearchProvider,
    SerpAPISearchProvider,
    SerperSearchProvider,
    GoogleCSEProvider,
)
from app.engines.discovery.company.company_validator import validate_companies


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company(
    name: str = "ABC Construction",
    website: str = "https://abcconstruction.com",
    source: str = "bing",
    confidence: float = 0.6,
) -> CompanyDiscoveryResult:
    return CompanyDiscoveryResult(
        company_name=name,
        website=website,
        source=source,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# company_cleaner tests
# ---------------------------------------------------------------------------


class TestCleanCompanies:

    def test_returns_empty_list_for_empty_input(self):
        cleaned, metrics = clean_companies([])
        assert cleaned == []
        assert metrics.total_cleaned == 0

    def test_removes_duplicate_domains(self):
        companies = [
            _make_company("ABC Construction Inc.", "https://abcconstruction.com"),
            _make_company("ABC Construction LLC", "https://www.abcconstruction.com"),
        ]
        cleaned, metrics = clean_companies(companies)
        assert len(cleaned) == 1
        assert metrics.total_cleaned == 1

    def test_removes_duplicate_names_after_normalization(self):
        companies = [
            _make_company("ACME Builders Inc.", "https://acme1.com"),
            _make_company("ACME Builders LLC", "https://acme2.com"),
        ]
        cleaned, metrics = clean_companies(companies)
        assert len(cleaned) == 1
        assert cleaned[0].company_name == "acme builders"

    def test_keeps_distinct_companies(self):
        companies = [
            _make_company("Alpha Constructors", "https://alpha.com"),
            _make_company("Beta Builders", "https://beta.com"),
            _make_company("Gamma General", "https://gamma.com"),
        ]
        cleaned, metrics = clean_companies(companies)
        assert len(cleaned) == 3
        assert metrics.total_cleaned == 3

    def test_normalizes_suffixes(self):
        companies = [
            _make_company("Test Co. Inc.", "https://test1.com"),
            _make_company("Builder Corp.", "https://test2.com"),
            _make_company("Acme Ltd.", "https://test3.com"),
        ]
        cleaned, _ = clean_companies(companies)
        assert "inc" not in cleaned[0].company_name.lower()
        assert "corp" not in cleaned[1].company_name.lower()
        assert "ltd" not in cleaned[2].company_name.lower()

    def test_errors_track_duplicates(self):
        companies = [
            _make_company("Dup Name", "https://dup.com"),
            _make_company("Dup Name Again", "https://dup.com"),
        ]
        _, metrics = clean_companies(companies)
        assert len(metrics.errors) == 1


# ---------------------------------------------------------------------------
# company_validator tests
# ---------------------------------------------------------------------------


class TestValidateCompanies:

    def test_skips_empty_name(self):
        companies = [_make_company(name="", website="https://example.com")]
        validated, metrics = validate_companies(companies)
        assert len(validated) == 0
        assert any("Invalid name" in e for e in metrics.errors)

    def test_skips_short_name(self):
        companies = [_make_company(name="AB", website="https://example.com")]
        validated, _ = validate_companies(companies)
        assert len(validated) == 0

    @patch("app.engines.discovery.company.company_validator.requests.head")
    def test_allows_valid_name(self, mock_head):
        resp = MagicMock()
        resp.status_code = 200
        mock_head.return_value = resp
        companies = [_make_company(name="Valid Builder LLC", website="https://valid.com")]
        validated, _ = validate_companies(companies)
        assert len(validated) == 1
        assert validated[0].confidence > 0.5

    @patch("app.engines.discovery.company.company_validator.requests.head")
    def test_removes_dead_website(self, mock_head):
        from requests import RequestException
        mock_head.side_effect = RequestException("connection refused")
        companies = [_make_company(name="Dead Co", website="https://dead.example")]
        validated, metrics = validate_companies(companies)
        assert len(validated) == 0
        assert any("Dead website" in e for e in metrics.errors)

    @patch("app.engines.discovery.company.company_validator.requests.head")
    def test_allows_live_website(self, mock_head):
        resp = MagicMock()
        resp.status_code = 200
        mock_head.return_value = resp
        companies = [_make_company(name="Live Builder", website="https://live.example")]
        validated, _ = validate_companies(companies)
        assert len(validated) == 1

    @patch("app.engines.discovery.company.company_validator.requests.head")
    def test_blocks_wikipedia_domains(self, mock_head):
        resp = MagicMock()
        resp.status_code = 200
        mock_head.return_value = resp
        companies = [_make_company(name="Wikipedia", website="https://en.wikipedia.org/wiki/Test")]
        validated, metrics = validate_companies(companies)
        assert len(validated) == 0
        assert any("Blocked domain" in e for e in metrics.errors)


# ---------------------------------------------------------------------------
# Provider availability & priority tests
# ---------------------------------------------------------------------------


class TestProviderAvailability:
    """Test that each provider reports availability correctly."""

    @patch.dict("os.environ", {}, clear=True)
    def test_serper_unavailable_without_key(self):
        p = SerperSearchProvider()
        assert p.available() is False
        assert p.name == "serper"
        assert p.priority == 1

    @patch.dict("os.environ", {"SERPER_API_KEY": "abc123"}, clear=True)
    def test_serper_available_with_key(self):
        p = SerperSearchProvider()
        assert p.available() is True

    @patch.dict("os.environ", {}, clear=True)
    def test_serpapi_unavailable_without_key(self):
        p = SerpAPISearchProvider()
        assert p.available() is False
        assert p.name == "serpapi"

    @patch.dict("os.environ", {"SERPAPI_API_KEY": "abc123"}, clear=True)
    def test_serpapi_available_with_key(self):
        p = SerpAPISearchProvider()
        assert p.available() is True

    @patch.dict("os.environ", {}, clear=True)
    def test_google_cse_unavailable_without_credentials(self):
        p = GoogleCSEProvider()
        assert p.available() is False

    @patch.dict(
        "os.environ",
        {"GOOGLE_CSE_API_KEY": "key", "GOOGLE_CSE_ID": "cx"},
        clear=True,
    )
    def test_google_cse_available_with_credentials(self):
        p = GoogleCSEProvider()
        assert p.available() is True

    def test_bing_always_available(self):
        p = BingSearchProvider()
        assert p.available() is True
        assert p.name == "bing"
        assert p.priority == 4

    def test_duckduckgo_always_available(self):
        p = DuckDuckGoSearchProvider()
        assert p.available() is True
        assert p.name == "duckduckgo"
        assert p.priority == 5


# ---------------------------------------------------------------------------
# search_companies — provider chain tests
# ---------------------------------------------------------------------------


class TestSearchCompanies:
    """Tests for the search_companies function and provider chain."""

    @patch.dict("os.environ", {}, clear=True)
    def test_returns_structured_diagnostic_when_no_providers_configured(self):
        """When no API keys are set, a Diagnostic object must be returned,
        not a bare empty list — so callers know exactly what happened."""
        results, metrics, diagnostic = search_companies(
            industry="Construction Estimating",
            location="Texas",
            limit=10,
        )
        assert isinstance(results, list)
        assert results == []
        assert isinstance(diagnostic, DiscoveryDiagnostic)
        assert diagnostic.to_dict()["status"] == "no_provider_available"
        skipped = diagnostic.providers_skipped
        assert any(s["provider"] in ("serper", "serpapi", "google_cse") for s in skipped)

    @patch.dict("os.environ", {}, clear=True)
    @patch.object(BingSearchProvider, "search", return_value=([], False))
    @patch.object(DuckDuckGoSearchProvider, "search", return_value=([], False))
    def test_diagnostic_on_captcha_failure(self, mock_ddg, mock_bing):
        """When free HTML providers all return empty (simulating CAPTCHA),
        the diagnostic should list bing and duckduckgo as failed."""
        results, metrics, diagnostic = search_companies(
            industry="Construction", location="TX", limit=5
        )
        assert results == []
        assert isinstance(diagnostic, DiscoveryDiagnostic)
        assert "bing" in diagnostic.providers_attempted
        assert "duckduckgo" in diagnostic.providers_attempted
        reason = diagnostic.to_dict()["reason"].lower()
        assert "none returned results" in reason

    @patch.dict("os.environ", {"SERPAPI_API_KEY": "demo-key"}, clear=True)
    @patch("requests.get")
    def test_provider_success(self, mock_get):
        """SerpAPI provider returns valid results → pipeline produces discoveries."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "organic_results": [
                {
                    "title": "ABC Builders TX",
                    "link": "https://abcbuilders.com",
                    "snippet": "Top contractor",
                },
                {
                    "title": "Dallas Construction Co",
                    "link": "https://dallasconstruction.com",
                    "snippet": "",
                },
            ],
        }
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results, metrics, diagnostic = search_companies(
            industry="Construction", location="Texas", limit=10
        )
        assert len(results) == 2
        assert diagnostic is None
        assert results[0].source == "serpapi"
        assert results[1].discovery_reason != ""
        assert results[0].source_url == results[0].website

    @patch.dict("os.environ", {}, clear=True)
    @patch.object(BingSearchProvider, "search")
    def test_filters_wikipedia_and_gov_urls(self, mock_bing):
        """Wikipedia and .gov URLs must never appear in results."""
        mock_bing.return_value = (
            [
                {
                    "title": "Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/Construction",
                    "snippet": "",
                },
                {
                    "title": "Gov Site",
                    "url": "https://example.gov/page",
                    "snippet": "",
                },
                {
                    "title": "Real Builder",
                    "url": "https://realbuilder.com",
                    "snippet": "Local contractor",
                },
            ],
            False,
        )
        results, _, _ = search_companies("Construction", "Texas", limit=10)
        for r in results:
            assert "wikipedia.org" not in r.website.lower()
            assert ".gov/" not in r.website.lower()
        assert len(results) == 1
        assert results[0].company_name == "Real Builder"

    @patch.dict("os.environ", {}, clear=True)
    @patch.object(BingSearchProvider, "search")
    def test_timeout_handling(self, mock_bing):
        """A provider that raises on search must not crash the pipeline."""
        from requests import RequestException
        mock_bing.side_effect = RequestException("read timed out")
        results, metrics, diagnostic = search_companies("Construction", "TX", limit=5)
        assert results == []
        assert isinstance(diagnostic, DiscoveryDiagnostic)
        assert "bing" in diagnostic.providers_attempted
        diag_dict = diagnostic.to_dict()
        assert "read timed out" in diag_dict["errors"]["bing"].lower()

    @patch.dict("os.environ", {}, clear=True)
    @patch.object(SerpAPISearchProvider, "search", return_value=([], False))
    @patch.object(BingSearchProvider, "search", return_value=([], False))
    def test_skips_unconfigured_providers(self, mock_bing, mock_serpapi):
        """Providers without credentials must be skipped silently
        (not counted as failed attempts)."""
        results, metrics, diagnostic = search_companies("Construction", "TX", limit=5)
        assert isinstance(diagnostic, DiscoveryDiagnostic)
        skipped_names = {s["provider"] for s in diagnostic.providers_skipped}
        assert "serpapi" in skipped_names
        # Bing is free so it should still be attempted
        assert "bing" in diagnostic.providers_attempted

    @patch.dict("os.environ", {"SERPAPI_API_KEY": "demo"}, clear=True)
    @patch("requests.get")
    def test_logs_provider_timing(self, mock_get, caplog):
        """Each provider call should be logged."""
        caplog.set_level(logging.INFO)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "organic_results": [
                {"title": "Test Builder", "link": "https://test.com", "snippet": "info"}
            ]
        }
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        search_companies("Construction", "TX", limit=5)
        assert any("serpapi" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Integration: full pipeline
# ---------------------------------------------------------------------------


class TestFullPipeline:

    @patch("app.engines.discovery.company.company_discovery_engine.search_companies")
    def test_pipeline_returns_clean_results(self, mock_search):
        """Successful search → validate → clean flow."""
        mock_search.return_value = (
            [
                _make_company("Alpha Builders", "https://alpha-builders.com", source="serpapi"),
                _make_company("Beta Contractors", "https://beta-contractors.com", source="serpapi"),
                _make_company(
                    "Alpha Builders", "https://alpha-builders.com", source="serpapi"
                ),  # dup
            ],
            DiscoveryMetrics(total_found=3),
            None,
        )
        from app.engines.discovery.company.company_discovery_engine import CompanyDiscoveryEngine

        engine = CompanyDiscoveryEngine()
        with patch(
            "app.engines.discovery.company.company_discovery_engine.validate_companies"
        ) as mock_validate, patch(
            "app.engines.discovery.company.company_discovery_engine.clean_companies"
        ) as mock_clean:
            mock_validate.return_value = (
                [_make_company("Alpha Builders", "https://alpha-builders.com")],
                DiscoveryMetrics(total_found=1, total_validated=1),
            )
            mock_clean.return_value = (
                [_make_company("Alpha Builders", "https://alpha-builders.com")],
                DiscoveryMetrics(total_found=1, total_cleaned=1),
            )
            results, metrics, diagnostic = engine.discover(
                industry="Construction Estimating",
                location="Dallas Texas",
                limit=10,
            )
            assert isinstance(results, list)
            assert isinstance(metrics, DiscoveryMetrics)
            assert diagnostic is None
            mock_search.assert_called_once()

    def test_pipeline_returns_diagnostic_on_search_failure(self):
        """When search returns a diagnostic, the pipeline short-circuits
        and propagates it — no empty silent return."""
        from app.engines.discovery.company.company_discovery_engine import CompanyDiscoveryEngine

        engine = CompanyDiscoveryEngine()
        diag = DiscoveryDiagnostic(
            providers_attempted=["bing", "duckduckgo"],
            providers_skipped=[{"provider": "serper", "reason": "not configured"}],
            errors={"bing": "CAPTCHA", "duckduckgo": "CAPTCHA"},
        )
        with patch(
            "app.engines.discovery.company.company_discovery_engine.search_companies"
        ) as mock_search:
            mock_search.return_value = ([], DiscoveryMetrics(errors=["fail"]), diag)
            results, metrics, returned_diag = engine.discover(
                "Construction", "TX", limit=5
            )
            assert results == []
            assert returned_diag is diag
            assert returned_diag.to_dict()["status"] == "no_provider_available"

    def test_metrics_accumulate_errors(self):
        """Metrics errors list should contain errors from all stages."""
        from app.engines.discovery.company.company_discovery_engine import CompanyDiscoveryEngine

        engine = CompanyDiscoveryEngine()
        diag = DiscoveryDiagnostic(
            providers_attempted=[],
            providers_skipped=[{"provider": "serpapi", "reason": "missing key"}],
            errors={"serpapi": "not configured"},
        )
        with patch(
            "app.engines.discovery.company.company_discovery_engine.search_companies"
        ) as mock_search:
            mock_search.return_value = ([], DiscoveryMetrics(errors=["source-fail"]), diag)
            results, metrics, returned_diag = engine.discover(
                "Construction", "TX", limit=5
            )
            assert results == []
            assert len(metrics.errors) >= 1
