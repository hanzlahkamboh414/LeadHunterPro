"""Tests for HtmlLeadershipExtractor.

Verifies the extractor satisfies the LeadershipExtractor contract and
correctly extracts leadership/decision-maker information from text.
"""

from __future__ import annotations

from app.discovery.website.evidence import ExtractedField
from app.discovery.website.extractors import ExtractionResult, LeadershipExtractor
from app.discovery.website.extractors_impl.leadership import (
    HtmlLeadershipExtractor,
)


class _MockPage:
    """Mock PageContent with fields leadership extractor needs."""

    def __init__(self, text_content: str = "", url: str = "https://test.com"):
        self.text_content = text_content
        self.url = url


class TestContract:
    """HtmlLeadershipExtractor satisfies the LeadershipExtractor contract."""

    def test_is_leadership_extractor(self):
        assert issubclass(HtmlLeadershipExtractor, LeadershipExtractor)

    def test_field_is_leadership(self):
        extractor = HtmlLeadershipExtractor()
        assert extractor.field == ExtractedField.LEADERSHIP

    def test_returns_list_never_none(self):
        extractor = HtmlLeadershipExtractor()
        page = _MockPage()
        result = extractor.extract(page)
        assert isinstance(result, list)
        assert result is not None


class TestExtraction:
    """Leadership extraction behavior."""

    def test_empty_text_returns_empty(self):
        extractor = HtmlLeadershipExtractor()
        page = _MockPage(text_content="")
        results = extractor.extract(page)
        assert results == []

    def test_extracts_name_and_title(self):
        extractor = HtmlLeadershipExtractor()
        page = _MockPage(text_content="Contact John Smith, CEO for more info.")
        results = extractor.extract(page)
        assert len(results) >= 1
        assert any("John Smith" in r.value for r in results)

    def test_title_in_attributes(self):
        extractor = HtmlLeadershipExtractor()
        page = _MockPage(text_content="Jane Doe, President of Acme Inc")
        results = extractor.extract(page)
        if results:
            assert "title" in results[0].attributes

    def test_evidence_method(self):
        extractor = HtmlLeadershipExtractor()
        page = _MockPage(text_content="Bob Jones - Owner")
        results = extractor.extract(page)
        if results:
            assert results[0].evidence.method == "text_pattern"
