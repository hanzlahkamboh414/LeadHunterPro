"""Tests for HtmlAddressExtractor.

Verifies the extractor satisfies the AddressExtractor contract and
correctly extracts US-style addresses from text content.
"""

from __future__ import annotations

from app.discovery.website.evidence import ExtractedField
from app.discovery.website.extractors import AddressExtractor, ExtractionResult
from app.discovery.website.extractors_impl.address import HtmlAddressExtractor


class _MockPage:
    """Mock PageContent with fields address extractor needs."""

    def __init__(self, text_content: str = "", url: str = "https://test.com"):
        self.text_content = text_content
        self.url = url


class TestContract:
    """HtmlAddressExtractor satisfies the AddressExtractor contract."""

    def test_is_address_extractor(self):
        assert issubclass(HtmlAddressExtractor, AddressExtractor)

    def test_field_is_address(self):
        extractor = HtmlAddressExtractor()
        assert extractor.field == ExtractedField.ADDRESS

    def test_returns_list_never_none(self):
        extractor = HtmlAddressExtractor()
        page = _MockPage()
        result = extractor.extract(page)
        assert result == []
        assert result is not None


class TestExtraction:
    """Address extraction behavior."""

    def test_empty_text_returns_empty(self):
        extractor = HtmlAddressExtractor()
        page = _MockPage(text_content="")
        results = extractor.extract(page)
        assert results == []

    def test_extracts_us_address(self):
        extractor = HtmlAddressExtractor()
        page = _MockPage(text_content="Visit us at 123 Main St, Dallas, TX 75201")
        results = extractor.extract(page)
        assert len(results) == 1
        assert isinstance(results[0], ExtractionResult)
        assert "123 Main St" in results[0].value
        assert "Dallas" in results[0].value
        assert "TX" in results[0].value

    def test_multiple_addresses(self):
        extractor = HtmlAddressExtractor()
        page = _MockPage(
            text_content="Office: 123 Main St, Dallas, TX 75201. "
            "Warehouse: 456 Elm Ave, Houston, TX 77001."
        )
        results = extractor.extract(page)
        assert len(results) == 2

    def test_evidence_method(self):
        extractor = HtmlAddressExtractor()
        page = _MockPage(text_content="123 Main St, Dallas, TX 75201")
        results = extractor.extract(page)
        assert results[0].evidence.method == "text_pattern"
