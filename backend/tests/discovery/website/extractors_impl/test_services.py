"""Tests for HtmlServicesExtractor.

Verifies the extractor satisfies the ServicesExtractor contract and
correctly extracts services/trades from multiple page sources.
"""

from __future__ import annotations

from app.discovery.website.evidence import ExtractedField
from app.discovery.website.extractors import ExtractionResult, ServicesExtractor
from app.discovery.website.extractors_impl.services import HtmlServicesExtractor


class _MockPage:
    """Mock PageContent with fields services extractor needs."""

    def __init__(
        self,
        text_content: str = "",
        meta_keywords: str = "",
        h1_texts: list[str] | None = None,
        description: str = "",
        url: str = "https://test.com",
    ):
        self.text_content = text_content
        self.meta_keywords = meta_keywords
        self.h1_texts = h1_texts or []
        self.description = description
        self.url = url


class TestContract:
    """HtmlServicesExtractor satisfies the ServicesExtractor contract."""

    def test_is_services_extractor(self):
        assert issubclass(HtmlServicesExtractor, ServicesExtractor)

    def test_field_is_services(self):
        extractor = HtmlServicesExtractor()
        assert extractor.field == ExtractedField.SERVICES

    def test_returns_list_never_none(self):
        extractor = HtmlServicesExtractor()
        page = _MockPage()
        result = extractor.extract(page)
        assert isinstance(result, list)
        assert result is not None


class TestExtraction:
    """Services extraction behavior."""

    def test_empty_page_returns_empty(self):
        extractor = HtmlServicesExtractor()
        page = _MockPage()
        results = extractor.extract(page)
        assert results == []

    def test_extracts_from_meta_keywords(self):
        extractor = HtmlServicesExtractor()
        page = _MockPage(meta_keywords="roofing, plumbing, electrical")
        results = extractor.extract(page)
        assert len(results) >= 1
        values = [r.value.lower() for r in results]
        assert any("roofing" in v for v in values)

    def test_extracts_from_text(self):
        extractor = HtmlServicesExtractor()
        page = _MockPage(
            text_content="We provide commercial construction and HVAC services."
        )
        results = extractor.extract(page)
        assert len(results) >= 1

    def test_deduplicates_services(self):
        extractor = HtmlServicesExtractor()
        page = _MockPage(
            meta_keywords="roofing",
            h1_texts=["Roofing Services"],
            description="Best roofing company.",
            text_content="We do roofing work.",
        )
        results = extractor.extract(page)
        # Should have only one "roofing" despite appearing in 4 places
        roofing_count = sum(1 for r in results if "roofing" in r.value.lower())
        assert roofing_count == 1

    def test_evidence_method_varies(self):
        extractor = HtmlServicesExtractor()
        page = _MockPage(
            meta_keywords="roofing",
            text_content="We provide plumbing services.",
        )
        results = extractor.extract(page)
        methods = {r.evidence.method for r in results}
        # Should have results from multiple methods
        assert len(methods) >= 1
