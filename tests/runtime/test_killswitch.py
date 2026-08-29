"""Tests for `runtime.killswitch`."""

from __future__ import annotations

from pathlib import Path

import pytest

from kalshi_bot.runtime.killswitch import KillSwitch, run_cli


def test_starts_disengaged(tmp_path: Path) -> None:
    assert not KillSwitch(tmp_path / "KILL", ttl_seconds=0).is_engaged()


def test_engage_then_release(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "KILL", ttl_seconds=0)

    switch.engage("manual halt during soak")
    assert switch.is_engaged()
    assert switch.reason() == "manual halt during soak"

    switch.release()
    assert not switch.is_engaged()
    assert switch.reason() is None


def test_release_is_idempotent(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "KILL", ttl_seconds=0)

    switch.release()
    switch.release()

    assert not switch.is_engaged()


def test_engage_creates_the_parent_directory(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "nested" / "KILL", ttl_seconds=0)

    switch.engage("why")

    assert switch.is_engaged()


def test_survives_a_restart(tmp_path: Path) -> None:
    # The whole point of a file over an in-memory flag: a halted bot must stay halted across the
    # supervisor restarting it, or a crash loop silently resumes trading.
    path = tmp_path / "KILL"
    KillSwitch(path, ttl_seconds=0).engage("halt")

    assert KillSwitch(path, ttl_seconds=0).is_engaged()


def test_an_externally_created_file_engages_it(tmp_path: Path) -> None:
    # Operators use `touch`, not this class. Presence is the contract, not anything written.
    path = tmp_path / "KILL"
    switch = KillSwitch(path, ttl_seconds=0)
    path.write_text("")

    assert switch.is_engaged()


def test_cache_serves_a_repeat_check_without_rereading(tmp_path: Path) -> None:
    path = tmp_path / "KILL"
    switch = KillSwitch(path, ttl_seconds=3600)

    assert not switch.is_engaged()
    path.write_text("engaged behind the cache\n")

    # Still reports the cached answer, the documented bound on operator-action latency.
    assert not switch.is_engaged()


def test_engaging_through_the_api_invalidates_the_cache(tmp_path: Path) -> None:
    # A process that engages its own switch must not then act on its own stale cache.
    switch = KillSwitch(tmp_path / "KILL", ttl_seconds=3600)
    assert not switch.is_engaged()

    switch.engage("tripped a limit")

    assert switch.is_engaged()


def test_releasing_through_the_api_invalidates_the_cache(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "KILL", ttl_seconds=3600)
    switch.engage("halt")
    assert switch.is_engaged()

    switch.release()

    assert not switch.is_engaged()


def test_cli_engage_records_the_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    switch = KillSwitch(tmp_path / "killswitch", ttl_seconds=0)

    exit_code = run_cli(["engage", "fills", "look", "wrong"], switch)

    assert exit_code == 0
    assert switch.is_engaged()
    assert switch.reason() == "fills look wrong"
    assert "ENGAGED" in capsys.readouterr().out


def test_cli_engage_without_a_reason_records_an_explicit_placeholder(tmp_path: Path) -> None:
    """An empty file is an unexplained halted bot, its own incident. The gap gets named."""
    switch = KillSwitch(tmp_path / "killswitch", ttl_seconds=0)

    assert run_cli(["engage"], switch) == 0
    assert switch.reason() == "engaged via scripts/killswitch.py, no reason given"


def test_cli_release_is_idempotent(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "killswitch", ttl_seconds=0)
    switch.engage("halt")

    assert run_cli(["release"], switch) == 0
    assert not switch.is_engaged()
    assert run_cli(["release"], switch) == 0  # releasing a released switch is fine


def test_cli_status_exit_code_distinguishes_engaged(tmp_path: Path) -> None:
    """`status` is exit-code-scriptable, 0 released and 2 engaged, so preflights need no parsing."""
    switch = KillSwitch(tmp_path / "killswitch", ttl_seconds=0)

    assert run_cli(["status"], switch) == 0
    switch.engage("halt")
    assert run_cli(["status"], switch) == 2


def test_cli_rejects_unknown_commands(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "killswitch", ttl_seconds=0)

    assert run_cli([], switch) == 1
    assert run_cli(["explode"], switch) == 1
    assert not switch.is_engaged()  # a usage error must not touch the switch
