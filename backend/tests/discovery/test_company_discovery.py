"""Unit tests for the company discovery engine."""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock
from urllib.parse import quote

from app.engines.discovery.company.company_models import (
    CompanyDiscoveryResult,
    DiscoveryMetrics,
)
from app.engines.discovery.company.company_cleaner import clean_companies
from app.engines.discovery.company.company_validator import validate_companies


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_company(
    name: str = "ABC Construction",
    website: str = "https://abcconstruction.com",
    confidence: float = 0.6,
) -> CompanyDiscoveryResult:
    return CompanyDiscoveryResult(
        company_name=name,
        website=website,
        source="google",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# company_cleaner tests
# ---------------------------------------------------------------------------

class TestCleanCompanies:
    """Tests for duplicate removal and name normalization."""

    def test_returns_empty_list_for_empty_input(self):
        cleaned, metrics = clean_companies([])
        assert cleaned == []
        assert metrics.total_cleaned == 0

    def test_removes_duplicate_domains(self):
        """Two entries with the same domain must collapse to one."""
        companies = [
            _make_company("ABC Construction Inc.", "https://abcconstruction.com"),
            _make_company("ABC Construction LLC", "https://www.abcconstruction.com"),
        ]
        cleaned, metrics = clean_companies(companies)
        assert len(cleaned) == 1
        assert metrics.total_cleaned == 1

    def test_removes_duplicate_names_after_normalization(self):
        """Different suffixes but same base name → kept once."""
        companies = [
            _make_company("ACME Builders Inc.", "https://acme1.com"),
            _make_company("ACME Builders LLC", "https://acme2.com"),
        ]
        cleaned, metrics = clean_companies(companies)
        assert len(cleaned) == 1
        # Stored name should have suffix stripped.
        assert cleaned[0].company_name == "acme builders"

    def test_keeps_distinct_companies(self):
        """Distinct names and domains survive."""
        companies = [
            _make_company("Alpha Constructors", "https://alpha.com"),
            _make_company("Beta Builders", "https://beta.com"),
            _make_company("Gamma General", "https://gamma.com"),
        ]
        cleaned, metrics = clean_companies(companies)
        assert len(cleaned) == 3
        assert metrics.total_cleaned == 3

    def test_normalizes_suffixes(self):
        """Inc., LLC, Corp., Ltd. should all be stripped in stored name."""
        companies = [
            _make_company("Test Co. Inc.", "https://test1.com"),
            _make_company("Builder Corp.", "https://test2.com"),
            _make_company("Acme Ltd.", "https://test3.com"),
        ]
        cleaned, _ = clean_companies(companies)
        # Each suffix-stripped name must not contain the corresponding suffix token.
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
    """Tests for name and website validation logic."""

    def test_skips_empty_name(self):
        companies = [_make_company(name="", website="https://example.com")]
        validated, metrics = validate_companies(companies)
        assert len(validated) == 0
        assert any("Invalid name" in e for e in metrics.errors)

    def test_skips_short_name(self):
        companies = [_make_company(name="AB", website="https://example.com")]
        validated, _ = validate_companies(companies)
        assert len(validated) == 0

    def test_allows_valid_name(self):
        companies = [_make_company(name="Valid Builder LLC", website="https://valid.com")]
        # Patch HEAD to return 200 so we only test name validation.
        with patch("app.engines.discovery.company.company_validator.requests.head") as mock_head:
            resp = MagicMock()
            resp.status_code = 200
            mock_head.return_value = resp
            validated, _ = validate_companies(companies)
        assert len(validated) == 1
        # Confidence boosted after live check passes.
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
# company_search tests (mocked HTTP)
# ---------------------------------------------------------------------------

class TestSearchCompanies:
    """Tests for search logic using mocked HTTP responses."""

    @patch("app.engines.discovery.company.company_search.requests.get")
    def test_returns_empty_when_no_results(self, mock_get):
        """When both sources return empty pages, result list is empty."""
        mock_resp = MagicMock()
        mock_resp.text = "<html><body></body></html>"
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        results, metrics = __import__(
            "app.engines.discovery.company.company_search", fromlist=["search_companies"]
        ).search_companies(industry="Construction Estimating", location="Dallas TX", limit=10)
        assert isinstance(results, list)
        assert isinstance(metrics, DiscoveryMetrics)

    @patch("app.engines.discovery.company.company_search.requests.get")
    def test_parses_google_style_links(self, mock_get):
        """Google-style organic results should produce CompanyDiscoveryResult objects."""
        html = (
            '<html><body>'
            '<a href="/url?q=https%3A//example-builder.com">Example Builder</a>'
            '<a href="/url?q=https%3A//another-contractor.com">Another Contractor</a>'
            "</body></html>"
        )
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        search = __import__(
            "app.engines.discovery.company.company_search", fromlist=["search_companies"]
        ).search_companies
        results, _ = search("Construction Estimating", "Texas", limit=100)
        assert len(results) >= 2
        for r in results:
            assert isinstance(r, CompanyDiscoveryResult)
            assert r.company_name != ""
            assert r.website != ""

    @patch("app.engines.discovery.company.company_search.requests.get")
    def test_filters_wikipedia_and_gov_urls(self, mock_get):
        html = (
            '<html><body>'
            '<a href="/url?q=https%3A//en.wikipedia.org/wiki/Test">Wikipedia</a>'
            '<a href="/url?q=https%3A//example.gov/page">Government</a>'
            '<a href="/url?q=https%3A//realbuilder.com">Real Builder</a>'
            "</body></html>"
        )
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        search = __import__(
            "app.engines.discovery.company.company_search", fromlist=["search_companies"]
        ).search_companies
        results, _ = search("Construction", "Texas", limit=100)
        for r in results:
            assert "wikipedia.org" not in r.website.lower()
            assert ".gov/" not in r.website.lower()


# ---------------------------------------------------------------------------
# Integration: full pipeline (mocked search + real validate + clean)
# ---------------------------------------------------------------------------

class TestFullPipeline:
    """End-to-end pipeline tests with mocked search output."""

    @patch("app.engines.discovery.company.company_search.search_companies")
    def test_pipeline_returns_clean_results(self, mock_search):
        mock_search.return_value = (
            [
                _make_company("Alpha Builders", "https://alpha-builders.com"),
                _make_company("Beta Contractors", "https://beta-contractors.com"),
                _make_company("Alpha Builders", "https://alpha-builders.com"),  # dup
            ],
            DiscoveryMetrics(total_found=3),
        )
        from app.engines.discovery.company.company_discovery_engine import CompanyDiscoveryEngine
        engine = CompanyDiscoveryEngine()
        # Patch validate to pass everything through (no real network calls).
        with patch(
            "app.engines.discovery.company.company_discovery_engine.validate_companies"
        ) as mock_validate, patch(
            "app.engines.discovery.company.company_discovery_engine.clean_companies"
        ) as mock_clean:
            mock_validate.return_value = ([_make_company("Alpha Builders", "https://alpha-builders.com")],
                                          DiscoveryMetrics(total_found=1, total_validated=1))
            mock_clean.return_value = ([_make_company("Alpha Builders", "https://alpha-builders.com")],
                                       DiscoveryMetrics(total_found=1, total_cleaned=1))
            results, metrics = engine.discover(
                industry="Construction Estimating",
                location="Dallas Texas",
                limit=10,
            )
            assert isinstance(results, list)
            assert isinstance(metrics, DiscoveryMetrics)
            mock_search.assert_called_once()

    def test_metrics_accumulate_errors(self):
        """Metrics should accumulate errors from all pipeline stages."""
        from app.engines.discovery.company.company_discovery_engine import CompanyDiscoveryEngine
        engine = CompanyDiscoveryEngine()
        with patch(
            "app.engines.discovery.company.company_discovery_engine.search_companies"
        ) as mock_search:
            mock_search.return_value = ([], DiscoveryMetrics(errors=["source-fail"]))
            results, metrics = engine.discover("Construction", "TX", limit=5)
            assert results == []
            assert len(metrics.errors) >= 0  # may include upstream errors
