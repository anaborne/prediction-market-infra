"""A single-instance lock, so two copies of a process cannot run against the same state.

Two executors would place duplicate orders for one wake; two pollers would double every wake.
Under a supervisor configured to restart on exit, the window for this is not hypothetical: a
process that is slow to die overlaps with its own replacement.

`fcntl.flock` is used rather than a pidfile because the kernel releases it. A pidfile written
by a process that is then `SIGKILL`ed, or that dies when the machine loses power, outlives the
process and blocks every subsequent start until someone removes it by hand, which is precisely the
situation an unattended restart cannot handle. A flock is released on process exit however that
exit happens, so a crash self-heals and a genuine overlap is still refused.

The lock is advisory and only binds processes that ask for it. That is sufficient here: the
processes it guards are the ones in this repository.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType


class AlreadyRunningError(RuntimeError):
    """Raised when another live process already holds the lock."""


class SingleInstanceLock:
    """An exclusive, non-blocking, kernel-released lock on a named file.

    Usable as a context manager. The lock file's contents are informational only, since the lock
    is held by the open file descriptor and by nothing written into it, but the PID is recorded
    so an operator looking at a refusal can find the process that caused it.

    Attributes:
        lock_path: Filesystem path of the lock file.
    """

    def __init__(self, lock_path: Path) -> None:
        """Store the lock path. Does not acquire anything yet.

        Args:
            lock_path: Filesystem path of the lock file. Its parent directory is created on
                acquire if missing.
        """
        self.lock_path = lock_path
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock, or raise if another process holds it.

        Raises:
            AlreadyRunningError: If another live process holds this lock.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise AlreadyRunningError(
                f"another process already holds {self.lock_path}. If you are certain none is "
                "running, check for a stale process instead of deleting the lock file. The "
                "lock lives in the kernel, so removing the file does not release it."
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        """Release the lock if held. Idempotent.

        Closing the descriptor is what releases the lock; the file is deliberately left in place,
        since unlinking it would let a second process create and lock a fresh file at the same
        path while this one still believes it holds the name.
        """
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    @property
    def held(self) -> bool:
        """Whether this object currently holds the lock."""
        return self._fd is not None

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
