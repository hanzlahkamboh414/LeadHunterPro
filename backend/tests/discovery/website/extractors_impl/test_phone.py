"""Tests for HtmlPhoneExtractor.

Verifies the extractor satisfies the PhoneExtractor contract and
correctly wraps ParsedPage.phones in evidence.
"""

from __future__ import annotations

from app.discovery.website.evidence import ExtractedField
from app.discovery.website.extractors import ExtractionResult, PhoneExtractor
from app.discovery.website.extractors_impl.phone import HtmlPhoneExtractor


class _MockPage:
    """Mock PageContent with only the fields phone extractor needs."""

    def __init__(self, phones: list[str], url: str = "https://test.com"):
        self.phones = phones
        self.url = url


class TestContract:
    """HtmlPhoneExtractor satisfies the PhoneExtractor contract."""

    def test_is_phone_extractor(self):
        assert issubclass(HtmlPhoneExtractor, PhoneExtractor)

    def test_field_is_phone(self):
        extractor = HtmlPhoneExtractor()
        assert extractor.field == ExtractedField.PHONE

    def test_returns_list_never_none(self):
        extractor = HtmlPhoneExtractor()
        page = _MockPage([])
        result = extractor.extract(page)
        assert result == []
        assert result is not None


class TestExtraction:
    """Phone extraction behavior."""

    def test_empty_phones_returns_empty(self):
        extractor = HtmlPhoneExtractor()
        page = _MockPage([])
        results = extractor.extract(page)
        assert results == []

    def test_single_phone(self):
        extractor = HtmlPhoneExtractor()
        page = _MockPage(["(555) 123-4567"])
        results = extractor.extract(page)
        assert len(results) == 1
        assert isinstance(results[0], ExtractionResult)
        assert results[0].value == "(555) 123-4567"
        assert results[0].evidence.field == ExtractedField.PHONE

    def test_multiple_phones(self):
        extractor = HtmlPhoneExtractor()
        page = _MockPage(["555-1234", "555-5678", "555-9999"])
        results = extractor.extract(page)
        assert len(results) == 3

    def test_evidence_carries_page_url(self):
        extractor = HtmlPhoneExtractor()
        page = _MockPage(["555-1234"], url="https://acme.com/contact")
        results = extractor.extract(page)
        assert results[0].evidence.page_url == "https://acme.com/contact"

    def test_evidence_method(self):
        extractor = HtmlPhoneExtractor()
        page = _MockPage(["555-1234"])
        results = extractor.extract(page)
        assert results[0].evidence.method == "html_parser_regex"
