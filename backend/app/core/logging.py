"""Structured logging configuration.

Provides a ``setup_logging`` helper that configures Python's standard
logging module with a jam-proof architecture:

* A :class:`~logging.handlers.QueueHandler` sits on the root logger so
  that **every** ``log.info(...)`` / ``log.error(...)`` call enqueues
  the record and returns immediately — it never blocks even when the
  console is frozen (Windows QuickEdit/select, redirected pipe stall,
  etc.).

* A background :class:`_QueueListenerThread` drains the queue and
  dispatches each record to one or more *sink* handlers.  By default
  the sinks are:

  1. A :class:`~logging.handlers.RotatingFileHandler` writing to
     ``backend/output/backend.log`` (max 10 MB × 5 backups) — the
     **reliable** destination that survives any console jam.
  2. A :class:`~logging.StreamHandler` on *stdout* — **only** when
     ``filelog_only`` is ``False`` (the default).  This gives the
     normal console experience during development but is *never* the
     sole destination, so a jammed console no longer freezes the
     pipeline.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import sys
import threading
from typing import Sequence


class _QueueListenerThread(threading.Thread):
    """Daemon thread that drains a :class:`logging.handlers.QueueHandler`
    queue and dispatches records to a set of sink handlers.

    Pulled from :pythondoc:`howto/logging-cookbook` with the simplification
    that this thread owns the sinks' ``emit`` calls (no ``handle`` pass
    through) and shuts down cleanly on interpreter exit.
    """

    def __init__(
        self,
        q: queue.Queue,  # type: ignore[type-arg]
        handlers: Sequence[logging.Handler],
    ) -> None:
        super().__init__(daemon=True, name="QueueListenerThread")
        self._queue = q
        self._handlers = handlers
        self._stopped = threading.Event()

    # ------------------------------------------------------------------
    def run(self) -> None:  # pragma: no cover – background thread
        while not self._stopped.is_set():
            try:
                record = self._queue.get(timeout=0.5)
            except Exception:  # noqa: BLE001 – timeout / interrupt
                continue
            if record is None:  # sentinel → shutdown
                break
            for handler in self._handlers:
                try:
                    handler.emit(record)
                except Exception:  # noqa: BLE001 – must not crash
                    pass

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stopped.set()
        try:
            self._queue.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Module-level state so we can tear down in tests if needed.
# ---------------------------------------------------------------------------
_listener: _QueueListenerThread | None = None
_sinks: list[logging.Handler] = []


def _output_dir() -> str:
    """Return ``backend/output/``, creating it if necessary."""
    path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "output")
    )
    os.makedirs(path, exist_ok=True)
    return path


def setup_logging(level: str = "INFO", filelog_only: bool = False) -> None:
    """Configure root logging with a jam-proof QueueHandler architecture.

    Args:
        level: Logging level string (e.g. ``"DEBUG"``, ``"INFO"``).
        filelog_only: When *True*, only the rotating file handler is
            attached (no stdout sink).  Useful on headless servers or
            inside CI where stdout may not be consumed.  Defaults to
            *False* so normal development gets console output.
    """
    global _listener, _sinks  # noqa: PLW0603 – module-level singletons

    # Prevent duplicate setup (e.g. import + explicit call).
    if _listener is not None:
        return

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    formatter = logging.Formatter(fmt)

    # --- Sinks ---------------------------------------------------------
    _sinks = []

    # 1. Always: rotating file (jam-proof, reliable).
    log_file = os.path.join(_output_dir(), "backend.log")
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    _sinks.append(file_handler)

    # 2. Optionally: stdout (convenient but can jam).
    if not filelog_only:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        _sinks.append(stream_handler)

    # --- QueueHandler → never blocks caller ----------------------------
    log_queue: queue.Queue = queue.Queue()  # type: ignore[type-arg]
    queue_handler = logging.handlers.QueueHandler(log_queue)
    root_logger.addHandler(queue_handler)

    # --- Listener thread (daemon — dies with the process) --------------
    _listener = _QueueListenerThread(log_queue, _sinks)
    _listener.start()


def teardown_logging() -> None:
    """Stop the listener thread and close sinks — for test teardown."""
    global _listener, _sinks  # noqa: PLW0603

    if _listener is not None:
        _listener.stop()
        _listener.join(timeout=2)
        _listener = None

    for handler in _sinks:
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
    _sinks.clear()

    # Remove the QueueHandler from the root logger.
    root = logging.getLogger()
    root.handlers = [
        h for h in root.handlers
        if not isinstance(h, logging.handlers.QueueHandler)
    ]
