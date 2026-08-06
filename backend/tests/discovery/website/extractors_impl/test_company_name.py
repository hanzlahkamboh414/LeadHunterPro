"""Tests for HtmlCompanyNameExtractor.

Verifies the extractor satisfies the CompanyNameExtractor contract and
correctly extracts company names from title, h1, and description.
"""

from __future__ import annotations

from app.discovery.website.evidence import ExtractedField
from app.discovery.website.extractors import CompanyNameExtractor, ExtractionResult
from app.discovery.website.extractors_impl.company_name import (
    HtmlCompanyNameExtractor,
)


class _MockPage:
    """Mock PageContent with fields company name extractor needs."""

    def __init__(
        self,
        title: str = "",
        h1_texts: list[str] | None = None,
        description: str = "",
        url: str = "https://test.com",
    ):
        self.title = title
        self.h1_texts = h1_texts or []
        self.description = description
        self.url = url


class TestContract:
    """HtmlCompanyNameExtractor satisfies the CompanyNameExtractor contract."""

    def test_is_company_name_extractor(self):
        assert issubclass(HtmlCompanyNameExtractor, CompanyNameExtractor)

    def test_field_is_company_name(self):
        extractor = HtmlCompanyNameExtractor()
        assert extractor.field == ExtractedField.COMPANY_NAME

    def test_returns_list_never_none(self):
        extractor = HtmlCompanyNameExtractor()
        page = _MockPage()
        result = extractor.extract(page)
        assert isinstance(result, list)
        assert result is not None


class TestExtraction:
    """Company name extraction behavior."""

    def test_empty_page_returns_empty(self):
        extractor = HtmlCompanyNameExtractor()
        page = _MockPage()
        results = extractor.extract(page)
        assert results == []

    def test_extracts_from_title(self):
        extractor = HtmlCompanyNameExtractor()
        page = _MockPage(title="Acme Roofing")
        results = extractor.extract(page)
        assert len(results) >= 1
        assert any("Acme Roofing" in r.value for r in results)

    def test_strips_title_boilerplate(self):
        extractor = HtmlCompanyNameExtractor()
        page = _MockPage(title="Acme Roofing | Home")
        results = extractor.extract(page)
        # Should have "Acme Roofing", not "Home"
        values = [r.value for r in results]
        assert "Acme Roofing" in values
        assert "Home" not in values

    def test_extracts_from_h1(self):
        extractor = HtmlCompanyNameExtractor()
        page = _MockPage(h1_texts=["Welcome to Acme Inc"])
        results = extractor.extract(page)
        assert len(results) >= 1
        assert any("Welcome to Acme Inc" in r.value for r in results)

    def test_extracts_from_description(self):
        extractor = HtmlCompanyNameExtractor()
        page = _MockPage(description="Best Builders is a leading contractor.")
        results = extractor.extract(page)
        assert len(results) >= 1

    def test_evidence_method_varies_by_source(self):
        extractor = HtmlCompanyNameExtractor()
        page = _MockPage(
            title="Acme Roofing",
            h1_texts=["About Acme"],
            description="Acme serves Texas.",
        )
        results = extractor.extract(page)
        methods = {r.evidence.method for r in results}
        # Should have results from multiple methods
        assert len(methods) > 1
