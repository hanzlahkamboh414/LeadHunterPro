"""Tests for BraveSearchGenerator.

Verifies the generator satisfies the CandidateGenerator contract,
reuses BraveSearchProvider, and never performs live network calls
during tests (the provider is stubbed).
"""

from __future__ import annotations

import logging

import pytest

from app.discovery.website.candidates import Candidate, CandidateGenerator
from app.discovery.website.confidence import Confidence
from app.discovery.website.generators.brave import BraveSearchGenerator
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
        provider="brave",
        query="test",
        status=status,  # type: ignore[arg-type]
        error=error,
    )


class _StubProvider:
    """Stub BraveSearchProvider that returns a canned response."""

    provider_name = "brave"

    def __init__(self, response: SearchResponse) -> None:
        self._response = response
        self.calls = 0
        self.last_query = None

    async def search(self, query):
        self.calls += 1
        self.last_query = query
        return self._response


def _generator_with(response: SearchResponse) -> BraveSearchGenerator:
    """Build a generator whose provider is stubbed."""
    gen = BraveSearchGenerator(api_key="test-key")
    gen._provider = _StubProvider(response)  # type: ignore[assignment]
    return gen


# ---------------------------------------------------------------------------
# Contract compliance
# ---------------------------------------------------------------------------


class TestContract:
    """BraveSearchGenerator satisfies the CandidateGenerator contract."""

    def test_is_candidate_generator(self):
        """Subclasses CandidateGenerator."""
        assert issubclass(BraveSearchGenerator, CandidateGenerator)

    def test_generator_name(self):
        """generator_name is the declared name."""
        gen = _generator_with(_make_response([]))
        assert gen.generator_name == "brave_search"

    def test_generate_keyword_only(self):
        """generate() requires keyword arguments."""
        gen = _generator_with(_make_response([]))
        with pytest.raises(TypeError):
            gen.generate("Roofing", "Dallas", 5)  # type: ignore[misc]

    def test_returns_list_never_none(self):
        """generate() returns a list, never None."""
        gen = _generator_with(_make_response([]))
        result = gen.generate(industry="Roofing", location="Dallas", limit=5)
        assert result == []
        assert result is not None


# ---------------------------------------------------------------------------
# Result mapping
# ---------------------------------------------------------------------------


class TestMapping:
    """SearchResult → Candidate mapping."""

    def test_maps_results_to_candidates(self):
        """Each search result becomes a Candidate."""
        gen = _generator_with(
            _make_response(
                ["https://acme-roofing.com", "https://best-builders.com"]
            )
        )
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert len(candidates) == 2
        assert all(isinstance(c, Candidate) for c in candidates)

    def test_candidate_carries_generator_name(self):
        """Every candidate is stamped with the generator name."""
        gen = _generator_with(_make_response(["https://acme-roofing.com"]))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert candidates[0].generator == "brave_search"

    def test_candidate_attributes(self):
        """Candidate attributes carry title, snippet, query, position."""
        gen = _generator_with(_make_response(["https://acme-roofing.com"]))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        attrs = candidates[0].attributes
        assert attrs["title"] == "Company 0"
        assert attrs["snippet"] == "Snippet 0"
        assert "Roofing" in attrs["query"]
        assert attrs["position"] == "1"

    def test_confidence_defaults_unknown(self):
        """Candidates default to UNKNOWN confidence (nothing visited yet)."""
        gen = _generator_with(_make_response(["https://acme-roofing.com"]))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert candidates[0].confidence == Confidence.UNKNOWN


# ---------------------------------------------------------------------------
# Limit handling
# ---------------------------------------------------------------------------


class TestLimit:
    """Limit is honoured."""

    def test_limit_truncates(self):
        """Never returns more than limit candidates."""
        urls = [f"https://company{i}.com" for i in range(10)]
        gen = _generator_with(_make_response(urls))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=3
        )
        assert len(candidates) == 3

    def test_limit_larger_than_results(self):
        """Fewer results than limit returns all of them."""
        gen = _generator_with(_make_response(["https://only-one.com"]))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=10
        )
        assert len(candidates) == 1


# ---------------------------------------------------------------------------
# Failure handling — never raises, never fixtures
# ---------------------------------------------------------------------------


class TestFailureHandling:
    """Errors produce empty lists, never exceptions or fixtures."""

    def test_provider_error_returns_empty(self):
        """Provider error status yields empty list."""
        gen = _generator_with(
            _make_response([], status="error", error="No API key configured")
        )
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert candidates == []

    def test_no_results_returns_empty(self):
        """Empty results yields empty list."""
        gen = _generator_with(_make_response([]))
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert candidates == []

    def test_search_exception_returns_empty(self):
        """An exception in the provider is caught, yields empty list."""

        class _RaisingProvider:
            provider_name = "brave"

            async def search(self, query):
                raise RuntimeError("network down")

        gen = BraveSearchGenerator(api_key="test-key")
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
        string has no usable form. The point of this test is that the
        unusable one is dropped without killing the batch.
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
    """Query building from industry + location."""

    def test_query_includes_industry_and_location(self):
        """The built query contains both industry and location."""
        provider = _StubProvider(_make_response([]))
        gen = BraveSearchGenerator(api_key="test-key")
        gen._provider = provider  # type: ignore[assignment]
        gen.generate(industry="Plumbing", location="Austin Texas", limit=5)
        assert provider.calls == 1
        assert "Plumbing" in provider.last_query.keywords
        assert "Austin Texas" in provider.last_query.keywords

    def test_single_search_call(self):
        """generate() makes exactly one search call."""
        provider = _StubProvider(_make_response(["https://a.com"]))
        gen = BraveSearchGenerator(api_key="test-key")
        gen._provider = provider  # type: ignore[assignment]
        gen.generate(industry="Roofing", location="Dallas", limit=5)
        assert provider.calls == 1
