"""Tests for retry engine."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.crawlers.exceptions import CrawlerRetryExceeded
from app.crawlers.retry import RetryEngine, connector_retry


class TestRetryEngine:
    """Test RetryEngine with exponential backoff."""

    def test_execute_succeeds_first_attempt(self):
        """Function succeeds on first attempt."""
        engine = RetryEngine(max_retries=3)
        mock_func = MagicMock(return_value="success")
        result = engine.execute(mock_func)
        assert result == "success"
        mock_func.assert_called_once()

    def test_execute_retries_on_failure(self):
        """Function is retried on transient failure."""
        engine = RetryEngine(max_retries=2, backoff_base=0.01)
        mock_func = MagicMock(side_effect=[ConnectionError("fail"), "success"])
        result = engine.execute(mock_func)
        assert result == "success"
        assert mock_func.call_count == 2

    def test_execute_exhausts_retries(self):
        """CrawlerRetryExceeded raised after all retries fail."""
        engine = RetryEngine(max_retries=2, backoff_base=0.01)
        mock_func = MagicMock(side_effect=ConnectionError("always fails"))
        with pytest.raises(CrawlerRetryExceeded):
            engine.execute(mock_func, url="https://example.com")
        assert mock_func.call_count == 3  # Initial + 2 retries

    def test_execute_does_not_retry_permanent_errors(self):
        """Non-retryable errors are not retried."""
        engine = RetryEngine(max_retries=3, retry_on=(ConnectionError,))
        mock_func = MagicMock(side_effect=ValueError("permanent error"))
        with pytest.raises(ValueError):
            engine.execute(mock_func)
        mock_func.assert_called_once()

    def test_should_retry_true_for_matching_error(self):
        """should_retry returns True for matching exception types."""
        engine = RetryEngine(retry_on=(ConnectionError, TimeoutError))
        assert engine.should_retry(ConnectionError()) is True
        assert engine.should_retry(TimeoutError()) is True

    def test_should_retry_false_for_non_matching_error(self):
        """should_retry returns False for non-matching exception types."""
        engine = RetryEngine(retry_on=(ConnectionError,))
        assert engine.should_retry(ValueError()) is False


class TestConnectorRetryDecorator:
    """Test connector_retry decorator."""

    def test_decorator_succeeds_immediately(self):
        """Successfully called function returns immediately."""

        @connector_retry(max_retries=3)
        def success_func():
            return "ok"

        assert success_func() == "ok"

    def test_decorator_retries_on_failure(self):
        """Decorated function retries on connection errors."""
        call_count = 0

        @connector_retry(max_retries=2, backoff_base=0.01)
        def failing_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("transient")
            return "finally_ok"

        result = failing_func()
        assert result == "finally_ok"
        assert call_count == 3

    def test_decorator_exhausts_retries(self):
        """Decorated function raises after all retries."""

        @connector_retry(max_retries=2, backoff_base=0.01)
        def always_fails():
            raise ConnectionError("persistent")

        with pytest.raises(CrawlerRetryExceeded):
            always_fails()
