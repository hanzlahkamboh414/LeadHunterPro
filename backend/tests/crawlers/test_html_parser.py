"""Tests for HTML parser."""

from __future__ import annotations

import pytest

from app.crawlers.html_parser import HTMLParser


class TestHTMLParser:
    """Test HTML parsing utilities."""

    @pytest.fixture
    def parser(self):
        """Create HTMLParser instance."""
        return HTMLParser()

    def test_parse_basic_html(self, parser):
        """Parser extracts basic elements from HTML."""
        html = """
        <html>
            <head><title>Test Page</title></head>
            <body>
                <h1>Main Heading</h1>
                <p>Contact us at info@example.com</p>
            </body>
        </html>
        """
        result = parser.parse(html, base_url="https://example.com")
        assert result.title == "Test Page"
        assert "Main Heading" in result.h1_texts

    def test_parse_empty_html_raises(self, parser):
        """Parser raises ValueError for empty HTML."""
        with pytest.raises(ValueError, match="empty"):
            parser.parse("")

    def test_parse_none_html_raises(self, parser):
        """Parser raises ValueError for None HTML."""
        with pytest.raises(ValueError, match="empty"):
            parser.parse(None)  # type: ignore[arg-type]

    def test_extract_emails(self, parser):
        """Email extraction works correctly."""
        html = """
        <html>
            <body>
                <a href="mailto:contact@example.com">Contact</a>
                <p>Email: support@test.org</p>
            </body>
        </html>
        """
        emails = parser.extract_emails(html)
        assert "contact@example.com" in emails
        assert "support@test.org" in emails

    def test_extract_phones(self, parser):
        """Phone extraction works correctly."""
        html = """
        <html>
            <body>
                <p>Call us: +1-555-123-4567</p>
                <p>Local: (555) 987-6543</p>
            </body>
        </html>
        """
        phones = parser.extract_phones(html)
        assert any("555" in p for p in phones)

    def test_extract_social_links(self, parser):
        """Social media link extraction works."""
        html = """
        <html>
            <body>
                <a href="https://linkedin.com/company/test">LinkedIn</a>
                <a href="https://twitter.com/test">Twitter</a>
            </body>
        </html>
        """
        result = parser.parse(html, base_url="https://example.com")
        assert "linkedin" in result.social_links
        assert "twitter" in result.social_links

    def test_extract_links(self, parser):
        """Link extraction resolves relative URLs."""
        html = """
        <html>
            <body>
                <a href="/about">About</a>
                <a href="https://other.com/page">Other</a>
                <a href="#section">Anchor</a>
            </body>
        </html>
        """
        result = parser.parse(html, base_url="https://example.com")
        assert "https://example.com/about" in result.links
        assert "https://other.com/page" in result.links
        # Anchor links should be excluded
        assert not any("#" in link for link in result.links)

    def test_extract_meta(self, parser):
        """Metadata extraction works."""
        html = """
        <html>
            <head>
                <title>My Title</title>
                <meta name="description" content="My Description">
                <meta name="keywords" content="key1, key2">
            </head>
        </html>
        """
        meta = parser.extract_meta(html)
        assert meta["title"] == "My Title"
        assert meta["description"] == "My Description"
        assert "key1" in meta["keywords"]

    def test_parsed_page_is_empty(self, parser):
        """ParsedPage.is_empty returns True for minimal HTML."""
        html = "<html><body></body></html>"
        result = parser.parse(html)
        assert result.is_empty() is True

    def test_parsed_page_not_empty(self, parser):
        """ParsedPage.is_empty returns False when data exists."""
        html = "<html><head><title>Title</title></head></html>"
        result = parser.parse(html)
        assert result.is_empty() is False

    def test_normalize_text_collapses_whitespace(self, parser):
        """Text normalization collapses multiple spaces."""
        raw = "Hello    World\n\nNew line"
        normalized = parser._normalize_text(raw)
        assert normalized == "Hello World New line"

    def test_parser_with_lxml_engine(self):
        """Parser works with lxml engine if available."""
        try:
            parser = HTMLParser(parser_engine="lxml")
            html = "<html><body><p>Test</p></body></html>"
            result = parser.parse(html)
            assert result.text_content == "Test"
        except ImportError:
            pytest.skip("lxml not installed")
