"""Non-blocking logging for the hot-path processes.

Ordinary `logging` handlers write synchronously, on the calling thread. In this codebase that
thread is the event loop, and one of the calls sits directly upstream of `send_wake()`, so a log
line emitted while the disk is busy, or while a log file is being rotated, adds its full latency
to the order path. That is the same failure telemetry avoids with a writer thread
(`docs/GUIDE.md`), reintroduced through a different door.

`QueueHandler` moves the work off the calling thread: it puts the record on an in-memory queue and
returns. A `QueueListener` thread owns the real handlers and does the I/O. Nothing on the event
loop ever waits for a file write.

Rotation is handled here rather than by the supervisor because `launchd` does not rotate
`StandardOutPath`, and an unrotated file on a multi-day run grows until the disk does not like it.
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import queue
import sys
from pathlib import Path
from typing import Final

_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
_MAX_BYTES: Final = 32 * 1024 * 1024
_BACKUP_COUNT: Final = 5

_listener: logging.handlers.QueueListener | None = None


def configure_logging(
    log_path: Path | None = None,
    level: int = logging.INFO,
    *,
    also_stderr: bool = True,
) -> None:
    """Route the root logger through a queue to a rotating file and/or stderr.

    Safe to call once per process, at startup, before any threads or tasks are created. Calling it
    again tears down the previous listener first, so a re-configuration in tests does not leak
    threads or accumulate handlers.

    Args:
        log_path: File to write to, rotated at 32 MB with 5 backups kept. Its parent directory is
            created if missing. `None` writes no file, which is what the test suite and one-shot
            scripts want.
        level: Root logger level.
        also_stderr: Whether to also emit to stderr. Keep this on under a supervisor that captures
            stderr, so a failure during startup, before the log file is open, is still visible.

    Raises:
        ValueError: If neither a file nor stderr is requested, which would silently discard every
            log record in the process.
    """
    if log_path is None and not also_stderr:
        raise ValueError("configure_logging would discard all output: pass log_path or also_stderr")

    shutdown_logging()

    handlers: list[logging.Handler] = []
    formatter = logging.Formatter(_FORMAT)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    if also_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    # Unbounded: a dropped log line is worse than the memory a backlog costs, and the listener
    # thread drains far faster than any realistic emit rate. If this ever does grow without bound
    # the cause is a stuck handler, which a bounded queue would hide rather than fix.
    record_queue: queue.Queue[logging.LogRecord | None] = queue.Queue()

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(logging.handlers.QueueHandler(record_queue))
    root.setLevel(level)

    global _listener
    _listener = logging.handlers.QueueListener(record_queue, *handlers, respect_handler_level=False)
    _listener.start()
    atexit.register(shutdown_logging)


def shutdown_logging() -> None:
    """Stop the listener thread, flushing anything still queued. Idempotent.

    Registered with `atexit` by `configure_logging`, so a normal exit flushes without the caller
    doing anything. Call it explicitly before a hard exit path that skips `atexit`.
    """
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None
