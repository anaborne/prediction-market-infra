"""Monotonic timing, and the guard that says when two processes' timings are comparable.

The poller stamps a detect timestamp and the executor measures against it, so the arithmetic
spans two processes. That is only valid while both readings come from the same monotonic clock,
which means the same host and the same boot. Across a reboot the counter restarts; across hosts
it was never related. Neither produces an error, just a plausible-looking wrong number, which is
the worst possible failure for a latency measurement.

`clock_domain()` exists so that comparison can be checked rather than assumed. Callers carry it
alongside the timestamp and drop the cross-process measurement when it does not match, rather
than reporting a value they cannot stand behind.
"""

from __future__ import annotations

import socket
import time
from typing import Final

# Boot-time estimates drift by small amounts as NTP adjusts the wall clock, so the estimate is
# quantized before it becomes an identity. The bucket is wide enough to absorb ordinary
# corrections and far narrower than any plausible uptime, so two processes on one boot agree and
# two boots do not collide.
_BOOT_QUANTUM_SECONDS: Final = 60


def monotonic_ns() -> int:
    """Return a monotonic timestamp in nanoseconds.

    Use this for every duration measurement. `time.time()` is wall-clock and can step backwards
    when NTP corrects it, which silently yields negative latencies.

    Note for long unattended runs on macOS: this counter does not advance while the machine is
    asleep, so a measured duration spanning a sleep is an undercount, not an overcount. The
    parent system's soak procedure held the machine awake for exactly this reason; those
    operations notes are not part of this extraction.
    """
    return time.perf_counter_ns()


def wall_clock_ms() -> int:
    """Return Unix epoch milliseconds, for telemetry columns that record *when* not *how long*."""
    return time.time_ns() // 1_000_000


def _boot_epoch_seconds() -> int:
    """Estimate the epoch second this host booted at, quantized.

    Derived as wall clock minus uptime rather than read from a platform-specific source, so the
    same code works on macOS (the soak host) and Linux (any future deployment) without a
    per-platform branch. Precision does not matter here: this value is only ever compared for
    equality against another process's copy taken on the same boot.
    """
    boot = time.time() - time.monotonic()
    return int(boot // _BOOT_QUANTUM_SECONDS) * _BOOT_QUANTUM_SECONDS


def clock_domain() -> str:
    """Identify the host and boot this process's monotonic clock belongs to.

    Two processes may compare `monotonic_ns()` readings only if their `clock_domain()` strings are
    equal. See the module docstring for why this is checked rather than assumed.

    Returns:
        An opaque identifier, e.g. `"studio.local:1787290000"`. Treat it as a token to compare,
        not as a value to parse.
    """
    return f"{socket.gethostname()}:{_boot_epoch_seconds()}"
