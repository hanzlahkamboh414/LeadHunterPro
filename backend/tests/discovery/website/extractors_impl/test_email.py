"""Tests for HtmlEmailExtractor.

Verifies the extractor satisfies the EmailExtractor contract and
correctly wraps ParsedPage.emails in evidence.
"""

from __future__ import annotations

from app.discovery.website.evidence import ExtractedField
from app.discovery.website.extractors import EmailExtractor, ExtractionResult
from app.discovery.website.extractors_impl.email import HtmlEmailExtractor


class _MockPage:
    """Mock PageContent with only the fields email extractor needs."""

    def __init__(self, emails: list[str], url: str = "https://test.com"):
        self.emails = emails
        self.url = url


class TestContract:
    """HtmlEmailExtractor satisfies the EmailExtractor contract."""

    def test_is_email_extractor(self):
        assert issubclass(HtmlEmailExtractor, EmailExtractor)

    def test_field_is_email(self):
        extractor = HtmlEmailExtractor()
        assert extractor.field == ExtractedField.EMAIL

    def test_returns_list_never_none(self):
        extractor = HtmlEmailExtractor()
        page = _MockPage([])
        result = extractor.extract(page)
        assert result == []
        assert result is not None


class TestExtraction:
    """Email extraction behavior."""

    def test_empty_emails_returns_empty(self):
        extractor = HtmlEmailExtractor()
        page = _MockPage([])
        results = extractor.extract(page)
        assert results == []

    def test_single_email(self):
        extractor = HtmlEmailExtractor()
        page = _MockPage(["info@acme.com"])
        results = extractor.extract(page)
        assert len(results) == 1
        assert isinstance(results[0], ExtractionResult)
        assert results[0].value == "info@acme.com"
        assert results[0].evidence.field == ExtractedField.EMAIL

    def test_multiple_emails(self):
        extractor = HtmlEmailExtractor()
        page = _MockPage(["info@acme.com", "sales@acme.com", "support@acme.com"])
        results = extractor.extract(page)
        assert len(results) == 3
        assert results[0].value == "info@acme.com"
        assert results[1].value == "sales@acme.com"
        assert results[2].value == "support@acme.com"

    def test_evidence_carries_page_url(self):
        extractor = HtmlEmailExtractor()
        page = _MockPage(["info@acme.com"], url="https://acme.com/contact")
        results = extractor.extract(page)
        assert results[0].evidence.page_url == "https://acme.com/contact"

    def test_evidence_method(self):
        extractor = HtmlEmailExtractor()
        page = _MockPage(["info@acme.com"])
        results = extractor.extract(page)
        assert results[0].evidence.method == "html_parser_regex"
