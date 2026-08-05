"""Tests for crawler exceptions."""

from __future__ import annotations

import pytest

from app.crawlers.exceptions import (
    CrawlerError,
    CrawlerHTTPError,
    CrawlerRetryExceeded,
    CrawlerRobotsBlocked,
    CrawlerTimeout,
)


class TestCrawlerExceptions:
    """Test exception hierarchy and behavior."""

    def test_crawler_error_is_exception(self):
        """CrawlerError must be a proper Exception subclass."""
        assert issubclass(CrawlerError, Exception)

    def test_crawler_timeout_is_crawler_error(self):
        """CrawlerTimeout must inherit from CrawlerError."""
        assert issubclass(CrawlerTimeout, CrawlerError)

    def test_crawler_http_error_is_crawler_error(self):
        """CrawlerHTTPError must inherit from CrawlerError."""
        assert issubclass(CrawlerHTTPError, CrawlerError)

    def test_crawler_retry_exceeded_is_crawler_error(self):
        """CrawlerRetryExceeded must inherit from CrawlerError."""
        assert issubclass(CrawlerRetryExceeded, CrawlerError)

    def test_crawler_robots_blocked_is_crawler_error(self):
        """CrawlerRobotsBlocked must inherit from CrawlerError."""
        assert issubclass(CrawlerRobotsBlocked, CrawlerError)

    def test_crawler_timeout_message(self):
        """CrawlerTimeout creates proper error message."""
        exc = CrawlerTimeout("Request timed out")
        assert "timed out" in str(exc).lower()

    def test_crawler_http_error_attributes(self):
        """CrawlerHTTPError stores status_code and url."""
        exc = CrawlerHTTPError(status_code=404, url="https://example.com")
        assert exc.status_code == 404
        assert exc.url == "https://example.com"
        assert "404" in exc.message

    def test_crawler_http_error_custom_message(self):
        """CrawlerHTTPError accepts custom message."""
        exc = CrawlerHTTPError(
            status_code=500, url="https://api.example.com", message="Server error"
        )
        assert exc.message == "Server error"

    def test_crawler_retry_exceeded_attributes(self):
        """CrawlerRetryExceeded stores url and last_error."""
        last_err = ConnectionError("Connection refused")
        exc = CrawlerRetryExceeded(url="https://example.com", last_error=last_err)
        assert exc.url == "https://example.com"
        assert exc.last_error == last_err
        assert "example.com" in str(exc)

    def test_crawler_robots_blocked_attributes(self):
        """CrawlerRobotsBlocked stores url and reason."""
        exc = CrawlerRobotsBlocked("https://example.com/page", "Disallowed path")
        assert exc.url == "https://example.com/page"
        assert exc.reason == "Disallowed path"
        assert "robots" in str(exc).lower()

    def test_can_catch_base_exception(self):
        """All crawler exceptions can be caught by CrawlerError."""
        with pytest.raises(CrawlerError):
            raise CrawlerTimeout("test")

        with pytest.raises(CrawlerError):
            raise CrawlerHTTPError(404, "https://example.com")

        with pytest.raises(CrawlerError):
            raise CrawlerRetryExceeded("https://example.com", Exception())

        with pytest.raises(CrawlerError):
            raise CrawlerRobotsBlocked("https://example.com")
