"""Tests for RetryManager and connector_retry decorator."""

from __future__ import annotations

import pytest

from app.engines.source_connectors.retry_manager import RetryManager, connector_retry


# ---------------------------------------------------------------------------
# RetryManager class tests
# ---------------------------------------------------------------------------


class TestRetryManager:

    def test_success_on_first_try(self):
        mgr = RetryManager(max_retries=3)
        result = mgr.execute(lambda: "ok")
        assert result == "ok"

    def test_success_after_failures(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise ValueError("transient")
            return "ok"

        mgr = RetryManager(max_retries=3, exceptions=(ValueError,))
        result = mgr.execute(flaky)
        assert result == "ok"
        assert len(calls) == 2

    def test_raises_after_exhausted_retries(self):
        call_count = [0]
        def always_fail():
            call_count[0] += 1
            raise RuntimeError("permanent fail")

        mgr = RetryManager(max_retries=1, exceptions=(RuntimeError,))
        with pytest.raises(RuntimeError, match="permanent fail"):
            mgr.execute(always_fail)
        assert call_count[0] == 2  # initial + 1 retry

    def test_does_not_retry_unlisted_exceptions(self):
        call_count = [0]
        def raises_typeerror():
            call_count[0] += 1
            raise TypeError("not retriable")

        mgr = RetryManager(max_retries=3, exceptions=(ValueError,))
        with pytest.raises(TypeError):
            mgr.execute(raises_typeerror)
        assert call_count[0] == 1  # no retry for unlisted exception

    def test_custom_backoff(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise ConnectionError("net")
            return "ok"

        mgr = RetryManager(max_retries=2, backoff_factor=0.01, exceptions=(ConnectionError,))
        result = mgr.execute(flaky)
        assert result == "ok"


class TestConnectorRetryDecorator:

    def test_decorator_without_parens(self):
        @connector_retry(max_retries=1, exceptions=(ValueError,))
        def fetch():
            fetch.count += 1
            if fetch.count < 2:
                raise ValueError("fail")
            return "done"
        fetch.count = 0

        result = fetch()
        assert result == "done"
        assert fetch.count == 2

    def test_decorator_without_parens_success(self):
        call_count = [0]

        @connector_retry
        def succeed():
            call_count[0] += 1
            return "ok"

        assert succeed() == "ok"
        assert call_count[0] == 1

    def test_decorator_raising_after_retries(self):
        call_count = [0]

        @connector_retry(max_retries=1, exceptions=(KeyError,))
        def never_succeeds():
            call_count[0] += 1
            raise KeyError("missing")

        with pytest.raises(KeyError):
            never_succeeds()
        assert call_count[0] == 2
