"""Tests for `runtime.logging_setup`.

Each test restores the root logger afterwards, because leaving a `QueueHandler` attached would route
every later test's logging through a stopped listener and swallow `caplog` output suite-wide.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from kalshi_bot.runtime import logging_setup


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    logging_setup.shutdown_logging()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_writes_records_to_the_log_file(tmp_path: Path) -> None:
    log_path = tmp_path / "bot.log"
    logging_setup.configure_logging(log_path, also_stderr=False)

    logging.getLogger("test").warning("hello from the poller")
    logging_setup.shutdown_logging()  # flushes the listener thread

    assert "hello from the poller" in log_path.read_text()


def test_creates_the_parent_directory(tmp_path: Path) -> None:
    log_path = tmp_path / "nested" / "logs" / "bot.log"
    logging_setup.configure_logging(log_path, also_stderr=False)

    logging.getLogger("test").warning("x")
    logging_setup.shutdown_logging()

    assert log_path.exists()


def test_root_handler_is_a_queue_handler(tmp_path: Path) -> None:
    # This is the actual claim: emitting a record must not do file I/O on the calling thread.
    logging_setup.configure_logging(tmp_path / "bot.log", also_stderr=False)

    handlers = logging.getLogger().handlers

    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.handlers.QueueHandler)


def test_reconfiguring_does_not_accumulate_handlers(tmp_path: Path) -> None:
    logging_setup.configure_logging(tmp_path / "one.log", also_stderr=False)
    logging_setup.configure_logging(tmp_path / "two.log", also_stderr=False)

    logging.getLogger("test").warning("second")
    logging_setup.shutdown_logging()

    assert len(logging.getLogger().handlers) == 1
    assert "second" in (tmp_path / "two.log").read_text()
    assert (tmp_path / "one.log").read_text() == ""


def test_respects_the_configured_level(tmp_path: Path) -> None:
    log_path = tmp_path / "bot.log"
    logging_setup.configure_logging(log_path, level=logging.WARNING, also_stderr=False)

    logging.getLogger("test").info("suppressed")
    logging.getLogger("test").warning("kept")
    logging_setup.shutdown_logging()

    contents = log_path.read_text()
    assert "suppressed" not in contents
    assert "kept" in contents


def test_shutdown_is_idempotent(tmp_path: Path) -> None:
    logging_setup.configure_logging(tmp_path / "bot.log", also_stderr=False)

    logging_setup.shutdown_logging()
    logging_setup.shutdown_logging()


def test_refuses_a_configuration_that_would_discard_everything() -> None:
    with pytest.raises(ValueError, match="discard all output"):
        logging_setup.configure_logging(None, also_stderr=False)


def test_stderr_only_configuration_is_allowed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging_setup.configure_logging(None, also_stderr=True)

    logging.getLogger("test").warning("to stderr")
    logging_setup.shutdown_logging()

    assert "to stderr" in capsys.readouterr().err
