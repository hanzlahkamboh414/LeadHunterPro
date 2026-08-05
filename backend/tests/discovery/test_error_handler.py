"""Tests for ErrorHandler."""

from __future__ import annotations

import pytest

from app.engines.source_connectors.error_handler import ErrorHandler


class TestErrorHandler:

    def test_no_errors_initially(self):
        h = ErrorHandler("test_conn")
        assert not h.has_errors
        assert h.errors == []
        assert h.warnings == []

    def test_add_error(self):
        h = ErrorHandler("test_conn")
        h.add_error("fetch", "connection refused")
        assert h.has_errors
        assert len(h.errors) == 1
        assert h.errors[0]["operation"] == "fetch"
        assert h.errors[0]["message"] == "connection refused"

    def test_add_warning(self):
        h = ErrorHandler("test_conn")
        h.add_warning("slow response")
        assert len(h.warnings) == 1
        assert h.warnings[0] == "slow response"
        assert not h.has_errors

    def test_summary(self):
        h = ErrorHandler("test_conn")
        h.add_error("parse", "invalid JSON")
        h.add_warning("deprecated endpoint")
        summary = h.summary()
        assert summary["connector"] == "test_conn"
        assert summary["error_count"] == 1
        assert summary["warning_count"] == 1
        assert "errors" in summary
        assert "warnings" in summary

    def test_clear(self):
        h = ErrorHandler("test_conn")
        h.add_error("fetch", "timeout")
        h.clear()
        assert not h.has_errors
        assert h.errors == []
        assert h.warnings == []

    def test_multiple_errors(self):
        h = ErrorHandler("test_conn")
        h.add_error("op1", "err1")
        h.add_error("op2", "err2")
        assert len(h.errors) == 2
        assert h.has_errors
