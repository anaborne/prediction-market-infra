"""Tests for `runtime.clock`."""

from __future__ import annotations

from kalshi_bot.runtime import clock


def test_monotonic_ns_never_goes_backwards() -> None:
    readings = [clock.monotonic_ns() for _ in range(100)]

    assert readings == sorted(readings)


def test_wall_clock_ms_is_a_plausible_epoch_millisecond() -> None:
    # Sanity bound rather than an exact value: anything outside this range means the units are
    # wrong (seconds or nanoseconds mistaken for milliseconds), which is the realistic bug.
    now = clock.wall_clock_ms()

    assert 1_700_000_000_000 < now < 3_000_000_000_000


def test_clock_domain_is_stable_within_a_process() -> None:
    # Two processes on one boot must agree, or every cross-process latency row gets discarded.
    assert clock.clock_domain() == clock.clock_domain()


def test_clock_domain_survives_wall_clock_jitter() -> None:
    # The boot estimate is wall clock minus uptime, so NTP corrections perturb it. Quantizing is
    # what keeps a correction from splitting one boot into two domains mid-run.
    assert clock._boot_epoch_seconds() % 60 == 0


def test_clock_domain_includes_the_hostname() -> None:
    import socket

    assert socket.gethostname() in clock.clock_domain()
