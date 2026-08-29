"""Tests for `telemetry.heartbeat` and the `heartbeats` upsert path in `TelemetryDB`."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from pathlib import Path

from kalshi_bot.telemetry.db import TelemetryDB
from kalshi_bot.telemetry.heartbeat import beat_forever


def _rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM heartbeats ORDER BY process").fetchall()
    finally:
        conn.close()


def test_record_heartbeat_upserts_one_row_per_process(tmp_path: Path) -> None:
    """The table holds current state: repeated beats overwrite, never accumulate."""
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.record_heartbeat("poller")
    db.record_heartbeat("poller")
    db.record_heartbeat("executor")
    db.close()

    rows = _rows(tmp_path / "telemetry.db")
    assert [row["process"] for row in rows] == ["executor", "poller"]
    for row in rows:
        assert row["pid"] > 0
        assert row["beat_at_ms"] >= row["started_at_ms"]
        assert row["queue_depth"] >= 0
        assert row["dropped_rows"] == 0


async def test_beat_forever_beats_immediately_then_on_interval(tmp_path: Path) -> None:
    """The first beat is immediate, so a fresh process is visible without waiting an interval."""
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()

    task = asyncio.create_task(beat_forever(db, "poller", interval_seconds=0.02))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    db.close()

    rows = _rows(tmp_path / "telemetry.db")
    assert len(rows) == 1
    assert rows[0]["process"] == "poller"
