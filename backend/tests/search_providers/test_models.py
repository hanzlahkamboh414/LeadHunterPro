"""Tests for search provider models."""

from __future__ import annotations

from app.search_providers.models import SearchQuery, SearchResponse, SearchResult


class TestSearchQuery:
    """Test SearchQuery dataclass."""

    def test_basic_query(self):
        """Basic query construction."""
        q = SearchQuery(keywords="roofing dallas")
        assert q.keywords == "roofing dallas"
        assert q.location == ""
        assert q.num_results == 10
        assert q.safe_search is True

    def test_query_with_location(self):
        """Query with location hint."""
        q = SearchQuery(keywords="plumbing", location="Houston TX", num_results=5)
        assert q.keywords == "plumbing"
        assert q.location == "Houston TX"
        assert q.num_results == 5

    def test_query_with_safe_search_off(self):
        """Query with safe search disabled."""
        q = SearchQuery(keywords="construction", safe_search=False)
        assert q.safe_search is False

    def test_query_immutable(self):
        """Query fields are immutable (frozen dataclass)."""
        q = SearchQuery(keywords="test")
        try:
            q.keywords = "modified"
            assert False, "Should not be able to modify frozen dataclass"
        except AttributeError:
            pass  # Expected


class TestSearchResult:
    """Test SearchResult dataclass."""

    def test_basic_result(self):
        """Basic result construction."""
        r = SearchResult(title="Test Company", url="https://example.com")
        assert r.title == "Test Company"
        assert r.url == "https://example.com"
        assert r.domain == "example.com"
        assert r.position == 0

    def test_result_with_position(self):
        """Result with explicit position."""
        r = SearchResult(
            title="Company",
            url="https://company.com",
            position=3,
        )
        assert r.position == 3

    def test_result_domain_extraction(self):
        """Domain is auto-extracted from URL."""
        r = SearchResult(
            title="Test",
            url="https://www.test-example.com/path",
        )
        assert r.domain == "test-example.com"

    def test_result_with_snippet(self):
        """Result with snippet text."""
        r = SearchResult(
            title="Company",
            url="https://company.com",
            snippet="Leading roofing contractor in Texas",
        )
        assert r.snippet == "Leading roofing contractor in Texas"


class TestSearchResponse:
    """Test SearchResponse dataclass."""

    def test_empty_response(self):
        """Empty response defaults."""
        resp = SearchResponse()
        assert resp.results == []
        assert resp.status == "success"
        assert resp.error == ""
        assert resp.latency_ms == 0.0

    def test_response_with_results(self):
        """Response populated with results."""
        results = [
            SearchResult(title="A", url="https://a.com"),
            SearchResult(title="B", url="https://b.com"),
        ]
        resp = SearchResponse(
            results=results,
            provider="test",
            query="test query",
            total_estimated=42,
            latency_ms=150.5,
        )
        assert len(resp.results) == 2
        assert resp.provider == "test"
        assert resp.query == "test query"
        assert resp.total_estimated == 42
        assert resp.latency_ms == 150.5

    def test_error_response(self):
        """Error response status."""
        resp = SearchResponse(
            provider="broken",
            query="test",
            error="Connection refused",
            status="error",
        )
        assert resp.status == "error"
        assert resp.error == "Connection refused"

    def test_partial_response(self):
        """Partial response when some providers succeed."""
        resp = SearchResponse(
            results=[SearchResult(title="X", url="https://x.com")],
            status="partial",
            error="Some providers failed",
        )
        assert resp.status == "partial"
        assert len(resp.results) == 1
