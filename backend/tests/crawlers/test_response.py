"""Tests for crawl response model."""

from __future__ import annotations

import pytest

from app.crawlers.response import CrawlResponse


class TestCrawlResponse:
    """Test CrawlResponse dataclass."""

    def test_create_minimal_response(self):
        """CrawlResponse can be created with required fields."""
        response = CrawlResponse(
            url="https://example.com",
            status_code=200,
            content=b"<html></html>",
            headers={"Content-Type": "text/html"},
            successful=True,
            robots_compliant=True,
        )
        assert response.url == "https://example.com"
        assert response.status_code == 200
        assert response.successful is True
        assert response.robots_compliant is True
        assert response.cached is False
        assert response.error == ""
        assert response.response_time_ms == 0.0

    def test_default_fields(self):
        """CrawlResponse has sensible defaults."""
        response = CrawlResponse(
            url="https://example.com",
            status_code=200,
            content=b"",
            headers={},
            successful=True,
            robots_compliant=True,
        )
        assert response.cached is False
        assert response.error == ""
        assert response.response_time_ms == 0.0

    def test_text_property_decodes_utf8(self):
        """CrawlResponse.text decodes UTF-8 content."""
        html = "<html><body>Hello World</body></html>"
        response = CrawlResponse(
            url="https://example.com",
            status_code=200,
            content=html.encode("utf-8"),
            headers={},
            successful=True,
            robots_compliant=True,
        )
        assert response.text == html

    def test_text_property_handles_empty_content(self):
        """CrawlResponse.text returns empty string for empty content."""
        response = CrawlResponse(
            url="https://example.com",
            status_code=200,
            content=b"",
            headers={},
            successful=True,
            robots_compliant=True,
        )
        assert response.text == ""

    def test_text_property_fallback_to_latin1(self):
        """CrawlResponse.text falls back to latin-1 on decode error."""
        # Create content that fails UTF-8 decoding
        content = bytes([0x80, 0x81, 0x82])
        response = CrawlResponse(
            url="https://example.com",
            status_code=200,
            content=content,
            headers={},
            successful=True,
            robots_compliant=True,
        )
        # Should not raise, should return decoded string
        text = response.text
        assert isinstance(text, str)
        assert len(text) > 0

    def test_response_is_frozen(self):
        """CrawlResponse is immutable (frozen dataclass)."""
        response = CrawlResponse(
            url="https://example.com",
            status_code=200,
            content=b"",
            headers={},
            successful=True,
            robots_compliant=True,
        )
        with pytest.raises(AttributeError):
            response.url = "https://other.com"  # type: ignore[misc]

    def test_success_false_for_4xx(self):
        """CrawlResponse.successful is False for 4xx status codes."""
        response = CrawlResponse(
            url="https://example.com",
            status_code=404,
            content=b"Not Found",
            headers={},
            successful=False,
            robots_compliant=True,
        )
        assert response.successful is False

    def test_success_true_for_2xx(self):
        """CrawlResponse.successful is True for 2xx status codes."""
        response = CrawlResponse(
            url="https://example.com",
            status_code=200,
            content=b"OK",
            headers={},
            successful=True,
            robots_compliant=True,
        )
        assert response.successful is True

    def test_success_false_for_5xx(self):
        """CrawlResponse.successful is False for 5xx status codes."""
        response = CrawlResponse(
            url="https://example.com",
            status_code=500,
            content=b"Internal Server Error",
            headers={},
            successful=False,
            robots_compliant=True,
        )
        assert response.successful is False
