"""Tests for HtmlSocialExtractor.

Verifies the extractor satisfies the SocialExtractor contract and
correctly wraps ParsedPage.social_links in evidence.
"""

from __future__ import annotations

from app.discovery.website.evidence import ExtractedField
from app.discovery.website.extractors import ExtractionResult, SocialExtractor
from app.discovery.website.extractors_impl.social import HtmlSocialExtractor


class _MockPage:
    """Mock PageContent with only the fields social extractor needs."""

    def __init__(self, social_links: dict[str, str], url: str = "https://test.com"):
        self.social_links = social_links
        self.url = url


class TestContract:
    """HtmlSocialExtractor satisfies the SocialExtractor contract."""

    def test_is_social_extractor(self):
        assert issubclass(HtmlSocialExtractor, SocialExtractor)

    def test_field_is_social(self):
        extractor = HtmlSocialExtractor()
        assert extractor.field == ExtractedField.SOCIAL

    def test_returns_list_never_none(self):
        extractor = HtmlSocialExtractor()
        page = _MockPage({})
        result = extractor.extract(page)
        assert result == []
        assert result is not None


class TestExtraction:
    """Social profile extraction behavior."""

    def test_empty_social_links_returns_empty(self):
        extractor = HtmlSocialExtractor()
        page = _MockPage({})
        results = extractor.extract(page)
        assert results == []

    def test_single_social_link(self):
        extractor = HtmlSocialExtractor()
        page = _MockPage({"facebook": "https://facebook.com/acme"})
        results = extractor.extract(page)
        assert len(results) == 1
        assert isinstance(results[0], ExtractionResult)
        assert results[0].value == "https://facebook.com/acme"
        assert results[0].evidence.field == ExtractedField.SOCIAL

    def test_multiple_social_links(self):
        extractor = HtmlSocialExtractor()
        page = _MockPage({
            "facebook": "https://facebook.com/acme",
            "twitter": "https://twitter.com/acme",
            "linkedin": "https://linkedin.com/company/acme",
        })
        results = extractor.extract(page)
        assert len(results) == 3

    def test_platform_in_attributes(self):
        extractor = HtmlSocialExtractor()
        page = _MockPage({"facebook": "https://facebook.com/acme"})
        results = extractor.extract(page)
        assert results[0].attributes["platform"] == "facebook"

    def test_evidence_carries_page_url(self):
        extractor = HtmlSocialExtractor()
        page = _MockPage(
            {"facebook": "https://facebook.com/acme"},
            url="https://acme.com/about",
        )
        results = extractor.extract(page)
        assert results[0].evidence.page_url == "https://acme.com/about"

    def test_evidence_method(self):
        extractor = HtmlSocialExtractor()
        page = _MockPage({"facebook": "https://facebook.com/acme"})
        results = extractor.extract(page)
        assert results[0].evidence.method == "html_parser_pattern"
