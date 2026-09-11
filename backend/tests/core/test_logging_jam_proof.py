"""P2 fix — jam-proof logging must never block on a frozen console.

Root cause (observed live): the producer/consumer threads froze forever when
they logged to a ``StreamHandler(sys.stdout)`` on a Windows console stuck in
QuickEdit/select mode. The fix routes every log call through a
``QueueHandler`` (enqueue-and-return — cannot block) drained by a background
listener thread into a rotating file (+ optional stdout sink).

These tests assert the ARCHITECTURE, offline: the root logger uses a
QueueHandler, the listener is alive, and the file sink actually receives
records even when the queue path is exercised.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading

from app.core.logging import _QueueListenerThread, setup_logging, teardown_logging


def test_setup_installs_queue_handler_and_listener():
    teardown_logging()  # clear any prior setup so setup_logging runs fresh
    try:
        setup_logging(level="INFO", filelog_only=True)
        root = logging.getLogger()
        qhandlers = [
            h for h in root.handlers
            if isinstance(h, logging.handlers.QueueHandler)
        ]
        assert qhandlers, "root logger must use a QueueHandler (jam-proof)"
        # Confirmed: at least one background listener thread is running.
        listeners = [
            t for t in threading.enumerate()
            if isinstance(t, _QueueListenerThread)
        ]
        assert listeners, "a QueueListenerThread must be draining the queue"
    finally:
        teardown_logging()


def test_log_records_reach_rotating_file(tmp_path, monkeypatch):
    from app.core import logging as logging_mod

    teardown_logging()  # clear prior setup so the monkeypatched path is used
    # Point the file sink at a temp path so the test can assert on it.
    monkeypatch.setattr(logging_mod, "_output_dir", lambda: str(tmp_path))
    try:
        setup_logging(level="INFO", filelog_only=True)
        logging.getLogger("test.jam").warning("jam-proof check %d", 42)
        # RotatingFileHandler opens the file at CREATION (delay=False), so
        # "file exists" is not "record landed". Poll the CONTENT until the
        # listener thread has drained + emitted, or timeout.
        marker = "jam-proof check 42"
        content = ""
        for _ in range(100):
            if (tmp_path / "backend.log").exists():
                content = (tmp_path / "backend.log").read_text(encoding="utf-8")
                if marker in content:
                    break
            threading.Event().wait(0.05)
        assert marker in content
        assert "test.jam" in content
    finally:
        teardown_logging()


def test_no_stream_handler_when_filelog_only():
    teardown_logging()  # clear any prior setup so setup_logging runs fresh
    try:
        setup_logging(level="INFO", filelog_only=True)
        # NOTE: RotatingFileHandler is a StreamHandler subclass — the check must
        # be "does any sink write to sys.stdout", not "is any sink a StreamHandler".
        from app.core.logging import _sinks
        assert not any(
            getattr(getattr(h, "stream", None), "fileno", lambda: -1)() == 1
            or getattr(h, "stream", None) is sys.stdout
            for h in _sinks
        ), "filelog_only must not attach a stdout sink (jam-prone)"
    finally:
        teardown_logging()