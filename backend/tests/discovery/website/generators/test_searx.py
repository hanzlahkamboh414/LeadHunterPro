"""Tests for SearXNGGenerator.

Verifies the generator satisfies the CandidateGenerator contract,
reuses SearXNGProvider, and never performs live network calls
during tests (the provider is stubbed).
"""

from __future__ import annotations

import logging

import pytest

from app.discovery.website.candidates import Candidate, CandidateGenerator
from app.discovery.website.confidence import Confidence
from app.discovery.website.generators.searx import SearXNGGenerator
from app.search_providers.models import SearchResponse, SearchResult

logger = logging.getLogger(__name__)


def _make_response(
    urls: list[str],
    *,
    status: str = "success",
    error: str = "",
) -> SearchResponse:
    """Build a SearchResponse from a list of URLs."""
    results = [
        SearchResult(
            title=f"Company {i}",
            url=url,
            snippet=f"Snippet {i}",
            position=i + 1,
        )
        for i, url in enumerate(urls)
    ]
    return SearchResponse(
        results=results,
        provider="searxng",
        query="test",
        status=status,  # type: ignore[arg-type]
        error=error,
    )


class _StubProvider:
    """Stub SearXNGProvider that returns a canned response."""

    provider_name = "searxng"

    def __init__(self, response: SearchResponse) -> None:
        self._response = response
        self.calls = 0
        self.last_query = None

    async def search(self, query):
        self.calls += 1
        self.last_query = query
        return self._response


def _generator_with(response: SearchResponse) -> SearXNGGenerator:
    """Build a generator with a base URL whose provider is stubbed."""
    gen = SearXNGGenerator(base_url="https://searx.test")
    gen._provider = _StubProvider(response)  # type: ignore[assignment]
    return gen


# ---------------------------------------------------------------------------
# Contract compliance
# ---------------------------------------------------------------------------


class TestContract:
    """SearXNGGenerator satisfies the CandidateGenerator contract."""

    def test_is_candidate_generator(self):
        assert issubclass(SearXNGGenerator, CandidateGenerator)

    def test_generator_name(self):
        gen = _generator_with(_make_response([]))
        assert gen.generator_name == "searxng"

    def test_generate_keyword_only(self):
        gen = _generator_with(_make_response([]))
        with pytest.raises(TypeError):
            gen.generate("Roofing", "Dallas", 5)  # type: ignore[misc]

    def test_returns_list_never_none(self):
        gen = _generator_with(_make_response([]))
        result = gen.generate(industry="Roofing", location="Dallas", limit=5)
        assert result == []
        assert result is not None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    """Base URL configuration behaviour."""

    def test_no_base_url_returns_empty(self, monkeypatch):
        """Missing base URL yields empty list, never a crash."""
        monkeypatch.delenv("SEARXNG_URL", raising=False)
        gen = SearXNGGenerator()
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert candidates == []

    def test_no_base_url_does_not_construct_provider(self, monkeypatch):
        """No provider is constructed when base URL is absent."""
        monkeypatch.delenv("SEARXNG_URL", raising=False)
        gen = SearXNGGenerator()
        gen.generate(industry="Roofing", location="Dallas", limit=5)
        assert gen._provider is None

    def test_base_url_from_env(self, monkeypatch):
        """Base URL is read from SEARXNG_URL env var."""
        monkeypatch.setenv("SEARXNG_URL", "https://searx.env")
        gen = SearXNGGenerator()
        assert gen._base_url == "https://searx.env"

    def test_explicit_base_url_wins(self, monkeypatch):
        """Constructor base URL overrides the environment."""
        monkeypatch.setenv("SEARXNG_URL", "https://searx.env")
        gen = SearXNGGenerator(base_url="https://searx.explicit")
        assert gen._base_url == "https://searx.explicit"


# ---------------------------------------------------------------------------
# Result mapping
# ---------------------------------------------------------------------------


class TestMapping:
    """SearchResult → Candidate mapping."""

    def test_maps_results_to_candidates(self):
        gen = _generator_with(
            _make_response(["https://acme.com", "https://best.com"])
        )
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert len(candidates) == 2
        assert all(isinstance(c, Candidate) for c in candidates)

    def test_candidate_carries_generator_name(self):
        gen = _generator_with(_make_response(["https://acme.com"]))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert candidates[0].generator == "searxng"

    def test_candidate_attributes(self):
        gen = _generator_with(_make_response(["https://acme.com"]))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        attrs = candidates[0].attributes
        assert attrs["title"] == "Company 0"
        assert attrs["snippet"] == "Snippet 0"
        assert "Roofing" in attrs["query"]
        assert attrs["position"] == "1"

    def test_confidence_defaults_unknown(self):
        gen = _generator_with(_make_response(["https://acme.com"]))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert candidates[0].confidence == Confidence.UNKNOWN


# ---------------------------------------------------------------------------
# Limit handling
# ---------------------------------------------------------------------------


class TestLimit:
    def test_limit_truncates(self):
        urls = [f"https://company{i}.com" for i in range(10)]
        gen = _generator_with(_make_response(urls))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=3
        )
        assert len(candidates) == 3

    def test_limit_larger_than_results(self):
        gen = _generator_with(_make_response(["https://only-one.com"]))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=10
        )
        assert len(candidates) == 1


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_provider_error_returns_empty(self):
        gen = _generator_with(
            _make_response([], status="error", error="instance down")
        )
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert candidates == []

    def test_no_results_returns_empty(self):
        gen = _generator_with(_make_response([]))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert candidates == []

    def test_search_exception_returns_empty(self):
        class _RaisingProvider:
            provider_name = "searxng"

            async def search(self, query):
                raise RuntimeError("network down")

        gen = SearXNGGenerator(base_url="https://searx.test")
        gen._provider = _RaisingProvider()  # type: ignore[assignment]
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert candidates == []

    def test_malformed_url_skipped(self):
        """A result with an unusable URL is skipped, not fatal.

        Validity is the frozen url_normalizer's call, not this generator's.
        It assumes DEFAULT_SCHEME for scheme-less input, so "not-a-url"
        normalizes to "https://not-a-url/" and is kept — only the empty
        string has no usable form.
        """
        gen = _generator_with(
            _make_response(["https://valid.com", "not-a-url", ""])
        )
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert len(candidates) == 2
        assert "valid.com" in candidates[0].url
        assert all(c.url for c in candidates)


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


class TestQuery:
    def test_query_includes_industry_and_location(self):
        provider = _StubProvider(_make_response([]))
        gen = SearXNGGenerator(base_url="https://searx.test")
        gen._provider = provider  # type: ignore[assignment]
        gen.generate(industry="Plumbing", location="Austin Texas", limit=5)
        assert provider.calls == 1
        assert "Plumbing" in provider.last_query.keywords
        assert "Austin Texas" in provider.last_query.keywords

    def test_single_search_call(self):
        provider = _StubProvider(_make_response(["https://a.com"]))
        gen = SearXNGGenerator(base_url="https://searx.test")
        gen._provider = provider  # type: ignore[assignment]
        gen.generate(industry="Roofing", location="Dallas", limit=5)
        assert provider.calls == 1
