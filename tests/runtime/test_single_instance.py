"""Tests for `runtime.single_instance`.

The second-holder cases run the competing acquire in a subprocess instead of a thread: `flock` is
per-open-file-description, and two `os.open` calls in one process would contend correctly, but
testing across a real process boundary is what the production claim actually is.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kalshi_bot.runtime.single_instance import AlreadyRunningError, SingleInstanceLock

_CHILD = """
import sys
from pathlib import Path
sys.path.insert(0, {src!r})
from kalshi_bot.runtime.single_instance import AlreadyRunningError, SingleInstanceLock

lock = SingleInstanceLock(Path({path!r}))
try:
    lock.acquire()
except AlreadyRunningError:
    print("REFUSED")
else:
    print("ACQUIRED")
"""


def _acquire_in_subprocess(lock_path: Path) -> str:
    src = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_CHILD).format(src=src, path=str(lock_path))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_acquire_then_release(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "run.lock")

    assert not lock.held
    lock.acquire()
    assert lock.held
    lock.release()
    assert not lock.held


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "run.lock")
    lock.acquire()

    lock.release()
    lock.release()

    assert not lock.held


def test_creates_the_parent_directory(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "nested" / "dir" / "run.lock")

    with lock:
        assert lock.lock_path.exists()


def test_records_the_holding_pid(tmp_path: Path) -> None:
    import os

    lock = SingleInstanceLock(tmp_path / "run.lock")

    with lock:
        assert lock.lock_path.read_text().strip() == str(os.getpid())


def test_a_second_process_is_refused_while_the_lock_is_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"

    with SingleInstanceLock(lock_path):
        assert _acquire_in_subprocess(lock_path) == "REFUSED"


def test_a_second_process_succeeds_once_the_lock_is_released(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    lock = SingleInstanceLock(lock_path)
    lock.acquire()
    lock.release()

    assert _acquire_in_subprocess(lock_path) == "ACQUIRED"


def test_the_kernel_releases_the_lock_when_a_holder_dies_abruptly(tmp_path: Path) -> None:
    # The reason this is flock and not a pidfile: a SIGKILLed process leaves a pidfile behind and
    # blocks every future start, which an unattended restart cannot recover from on its own.
    lock_path = tmp_path / "run.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys, time
                from pathlib import Path
                sys.path.insert(0, {src!r})
                from kalshi_bot.runtime.single_instance import SingleInstanceLock
                lock = SingleInstanceLock(Path({path!r}))
                lock.acquire()
                print("HELD", flush=True)
                time.sleep(60)
                """
            ).format(src=str(Path(__file__).resolve().parents[2] / "src"), path=str(lock_path)),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD"
        assert _acquire_in_subprocess(lock_path) == "REFUSED"
        holder.kill()
        holder.wait(timeout=30)
    finally:
        if holder.poll() is None:  # pragma: no cover - only on an unexpected test failure
            holder.kill()

    assert _acquire_in_subprocess(lock_path) == "ACQUIRED"


def test_context_manager_releases_on_exception(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "run.lock")

    with pytest.raises(RuntimeError, match="boom"), lock:
        raise RuntimeError("boom")

    assert not lock.held


def test_error_message_warns_against_deleting_the_lock_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    first = SingleInstanceLock(lock_path)
    first.acquire()
    try:
        # Same-process second acquire uses a distinct file description, so flock refuses it the
        # same way it refuses another process.
        with pytest.raises(AlreadyRunningError, match="does not release it"):
            SingleInstanceLock(lock_path).acquire()
    finally:
        first.release()
