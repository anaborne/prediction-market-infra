"""The process heartbeat loop.

Closes the gap the fail-loudly TaskGroup rule's fail-loudly change leaves open: `TaskGroup`
guarantees a dead *leg*
ends the process, so process-alive now implies legs-alive, though not legs-*making-progress*, and
not visible-to-anyone. Each trading process runs `beat_forever()` as one of its tasks, upserting
its row in the `heartbeats` table every `interval_seconds`; the dashboard's `/health` turns
`beat_at_ms` staleness into `ok`/`degraded`/`down` without importing anything write-capable.

The beat itself is the standard fire-and-forget enqueue (`TelemetryDB.record_heartbeat`), so the
loop costs the event loop nothing measurable. That has one honest consequence: a beat proves the
process's event loop *and* its telemetry writer thread are both alive, since a wedged writer stops
beats from landing even though the process runs, which for a monitoring signal is the right
failure direction (false-dead, never false-alive).
"""

from __future__ import annotations

import asyncio

from kalshi_bot.telemetry.db import TelemetryDB

DEFAULT_INTERVAL_SECONDS = 10.0

# /health calls a process dead when its last beat is older than this many intervals. Three, not
# two: one interval of ordinary jitter plus one full missed beat should not page anyone.
STALE_AFTER_INTERVALS = 3


async def beat_forever(
    telemetry_db: TelemetryDB,
    process: str,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Upsert `process`'s heartbeat row every `interval_seconds`, forever.

    The first beat is sent immediately, so a freshly-started process is visible to `/health`
    without waiting out an interval.

    Args:
        telemetry_db: The process's own (initialized) telemetry sink.
        process: Stable process name, e.g. `"poller"` or `"executor"`.
        interval_seconds: Time between beats.
    """
    while True:
        telemetry_db.record_heartbeat(process)
        await asyncio.sleep(interval_seconds)
