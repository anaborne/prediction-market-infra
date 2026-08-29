"""A file-presence kill switch: the operator's stop button for a running bot.

The mechanism is a file existing at a known path. That choice is deliberate, and the alternatives
were rejected for specific reasons:

- A dashboard button would require `dashboard` to gain a write path into the trading system,
  giving up the read-only guarantee that `docs/GUIDE.md` enforces structurally. It is also
  unauthenticated.
- A signal alone requires the process to be responsive enough to run its handler, which is not
  guaranteed for the case where you most want to stop it.
- A database flag requires SQLite to be reachable and the process to be polling it.

A file works from any shell, needs no cooperation from the running process beyond a `stat` call,
survives a restart, and can be checked by anything. `scripts/killswitch.py` is the operator
interface; this module is what the processes read.

Engaging halts *new* order dispatch. It does not cancel resting orders, since this bot places
none (`docs/GUIDE.md`), and does not stop ingest or telemetry, so a halted bot keeps
observing and recording.
"""

from __future__ import annotations

from pathlib import Path

from kalshi_bot.runtime.clock import monotonic_ns


class KillSwitch:
    """Checks for the kill-switch file, with a short cache so the hot path can poll it freely.

    A `stat` call is cheap but it is still a filesystem syscall. The executor consults this
    before every real dispatch (real fires arrive a few times an hour, so those checks pay the
    stat; the cache exists so any future higher-frequency caller, e.g. a per-wake poller check,
    would not turn it into hundreds of syscalls a second). The cache bounds that cost while
    keeping the worst-case delay between engaging the switch and the bot honoring it to
    `ttl_seconds`.

    The cache deliberately does not apply to the transition *into* the engaged state any faster than
    `ttl_seconds`, because there is no way to be notified without a watcher thread, and a sub-second
    stale window on a manual operator action is not worth one.

    Attributes:
        path: Filesystem path whose existence engages the switch.
        ttl_seconds: How long a check result is reused before re-reading the filesystem.
    """

    def __init__(self, path: Path, ttl_seconds: float = 1.0) -> None:
        """Store the switch path and cache duration.

        Args:
            path: Filesystem path whose existence engages the switch.
            ttl_seconds: How long a check result is reused. Pass `0` to disable caching, which
                tests do so they can observe a change immediately.
        """
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._cached: bool | None = None
        self._checked_at_ns = 0

    def is_engaged(self) -> bool:
        """Whether the kill switch is currently engaged, subject to the cache TTL."""
        now = monotonic_ns()
        if self._cached is not None and now - self._checked_at_ns < self.ttl_seconds * 1e9:
            return self._cached
        self._cached = self.path.exists()
        self._checked_at_ns = now
        return self._cached

    def engage(self, reason: str) -> None:
        """Create the kill-switch file, halting new dispatch.

        Args:
            reason: Free text recorded in the file so whoever finds it later knows why it is
                there. An unexplained halted bot is its own incident.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"{reason}\n")
        self._invalidate()

    def release(self) -> None:
        """Remove the kill-switch file, allowing dispatch again. Idempotent."""
        self.path.unlink(missing_ok=True)
        self._invalidate()

    def reason(self) -> str | None:
        """The text recorded when the switch was engaged, or `None` if it is not engaged."""
        try:
            return self.path.read_text().strip()
        except OSError:
            return None

    def _invalidate(self) -> None:
        self._cached = None
        self._checked_at_ns = 0


def run_cli(argv: list[str], kill_switch: KillSwitch) -> int:
    """The operator command line behind `scripts/killswitch.py`.

    Lives here instead of in the script so the interface is unit-testable. The script wrapper
    only builds the `KillSwitch` from config and passes `sys.argv` through.

    Commands:
        engage [reason...]: Create the switch file. The reason is recorded in it; an unexplained
            halted bot is its own incident, so an omitted reason gets an explicit placeholder
            naming the gap rather than an empty file.
        release: Remove the switch file. Idempotent.
        status: Print the current state. Exits `0` when released and `2` when engaged, so
            preflight scripts can gate on it without parsing output.

    Args:
        argv: Command-line arguments, excluding the program name.
        kill_switch: The switch to operate on.

    Returns:
        Process exit code: `0` on success (and on `status` of a released switch), `2` for
        `status` of an engaged switch, `1` for usage errors.
    """
    if not argv or argv[0] not in ("engage", "release", "status"):
        print("usage: killswitch.py {engage [reason...] | release | status}")
        return 1

    command = argv[0]
    if command == "engage":
        reason = " ".join(argv[1:]).strip() or "engaged via scripts/killswitch.py, no reason given"
        kill_switch.engage(reason)
        print(f"ENGAGED at {kill_switch.path}: {reason}")
        print("New order dispatch is halted. Ingest and telemetry keep running.")
        return 0
    if command == "release":
        was_engaged = kill_switch.is_engaged()
        kill_switch.release()
        print(f"RELEASED ({kill_switch.path} {'removed' if was_engaged else 'was not present'}).")
        return 0
    if kill_switch.is_engaged():
        print(f"ENGAGED at {kill_switch.path}: {kill_switch.reason() or '(no reason recorded)'}")
        return 2
    print(f"released ({kill_switch.path} not present)")
    return 0
