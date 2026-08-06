"""Tests for FixtureGenerator.

Verifies the generator satisfies the CandidateGenerator contract,
reuses FixtureSource (does not re-parse fixtures), and stamps every
candidate with fixture provenance (CLAUDE.md §1).
"""

from __future__ import annotations

import logging

import pytest

from app.discovery.website.candidates import Candidate, CandidateGenerator
from app.discovery.website.generators.fixture import FixtureGenerator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Contract compliance
# ---------------------------------------------------------------------------


class TestContract:
    """FixtureGenerator satisfies the CandidateGenerator contract."""

    def test_is_candidate_generator(self):
        assert issubclass(FixtureGenerator, CandidateGenerator)

    def test_generator_name(self):
        gen = FixtureGenerator()
        assert gen.generator_name == "fixture"

    def test_generate_keyword_only(self):
        gen = FixtureGenerator()
        with pytest.raises(TypeError):
            gen.generate("Roofing", "Dallas", 5)  # type: ignore[misc]

    def test_returns_list_never_none(self):
        gen = FixtureGenerator()
        result = gen.generate(
            industry="Roofing", location="Dallas Texas", limit=5
        )
        assert isinstance(result, list)
        assert result is not None


# ---------------------------------------------------------------------------
# Reuse — does not re-parse fixture
# ---------------------------------------------------------------------------


class TestReuse:
    """FixtureGenerator reuses FixtureSource (CLAUDE.md §14)."""

    def test_uses_fixture_source(self):
        """Generator holds a FixtureSource instance."""
        from app.discovery.sources.fixture_source import FixtureSource

        gen = FixtureGenerator()
        assert isinstance(gen._source, FixtureSource)

    def test_delegates_matching(self, monkeypatch):
        """generate() delegates to FixtureSource.discover()."""
        from app.discovery.sources.status import SourceStatus

        gen = FixtureGenerator()
        calls = {"n": 0}

        def _fake_discover(*, industry, location, limit):
            calls["n"] += 1
            return (
                SourceStatus.SUCCESS,
                [{"company_name": "Test Co", "website": "https://test.com"}],
                {"data_source": "fixture", "fallback_reason": "test"},
            )

        monkeypatch.setattr(gen._source, "discover", _fake_discover)
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert calls["n"] == 1
        assert len(candidates) == 1


# ---------------------------------------------------------------------------
# Provenance — fixture usage is never hidden
# ---------------------------------------------------------------------------


class TestProvenance:
    """Every candidate carries fixture provenance (CLAUDE.md §1)."""

    def test_real_fixture_candidates_tagged(self):
        """Candidates from the real fixture carry data_source=fixture."""
        gen = FixtureGenerator()
        candidates = gen.generate(
            industry="Roofing", location="Dallas Texas", limit=5
        )
        # Real fixture has Dallas roofing companies
        assert len(candidates) > 0
        for c in candidates:
            assert c.attributes["data_source"] == "fixture"
            assert "fallback_reason" in c.attributes
            assert c.generator == "fixture"

    def test_candidates_have_real_urls(self):
        """No synthetic/.example.com domains — real fixture URLs only."""
        gen = FixtureGenerator()
        candidates = gen.generate(
            industry="Roofing", location="Dallas Texas", limit=10
        )
        for c in candidates:
            assert ".example.com" not in c.url
            assert c.url.startswith("http")


# ---------------------------------------------------------------------------
# Limit handling
# ---------------------------------------------------------------------------


class TestLimit:
    def test_limit_respected(self):
        gen = FixtureGenerator()
        candidates = gen.generate(
            industry="Roofing", location="Texas", limit=3
        )
        assert len(candidates) <= 3


# ---------------------------------------------------------------------------
# Empty / no-match handling
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_no_match_returns_empty(self):
        """A non-matching query returns an empty list, not a crash."""
        gen = FixtureGenerator()
        candidates = gen.generate(
            industry="Nonexistent Industry ZZZ",
            location="Nowhere Antarctica",
            limit=5,
        )
        assert candidates == []

    def test_company_without_website_skipped(self, monkeypatch):
        """A fixture company with no website is skipped."""
        from app.discovery.sources.status import SourceStatus

        gen = FixtureGenerator()

        def _fake_discover(*, industry, location, limit):
            return (
                SourceStatus.SUCCESS,
                [
                    {"company_name": "No Site Co", "website": ""},
                    {"company_name": "Has Site", "website": "https://has.com"},
                ],
                {"data_source": "fixture", "fallback_reason": "test"},
            )

        monkeypatch.setattr(gen._source, "discover", _fake_discover)
        candidates = gen.generate(
            industry="Roofing", location="Dallas", limit=5
        )
        assert len(candidates) == 1
        assert "has.com" in candidates[0].url
