"""Tests for ConnectorLogger."""

from __future__ import annotations

import logging

from app.engines.source_connectors.connector_logger import ConnectorLogger


class TestConnectorLogger:

    def test_logger_path(self):
        log = ConnectorLogger("test_conn")
        assert log._logger.name == "connectors.test_conn"

    def test_info_method(self, caplog):
        log = ConnectorLogger("test_conn")
        with caplog.at_level(logging.INFO):
            log.info("hello %s", "world")
        assert "hello world" in caplog.text

    def test_warning_method(self, caplog):
        log = ConnectorLogger("test_conn")
        with caplog.at_level(logging.WARNING):
            log.warning("something happened")
        assert "something happened" in caplog.text

    def test_error_method(self, caplog):
        log = ConnectorLogger("test_conn")
        with caplog.at_level(logging.ERROR):
            log.error("failure occurred")
        assert "failure occurred" in caplog.text

    def test_error_with_exception(self, caplog):
        log = ConnectorLogger("test_conn")
        exc = ValueError("oops")
        with caplog.at_level(logging.ERROR):
            log.error("boom", exc=exc)
        assert "boom" in caplog.text
        assert "oops" in caplog.text

    def test_debug_method(self, caplog):
        log = ConnectorLogger("test_conn")
        with caplog.at_level(logging.DEBUG):
            log.debug("trace info")
        assert "trace info" in caplog.text
