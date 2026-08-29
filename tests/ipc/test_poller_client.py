"""Tests for `IPCPollerClient`.

Exercises the client against a minimal fake executor built with a real
`asyncio.start_unix_server`, with no `ExecutorServer` needed here, since only the client's own
non-blocking/reconnect/telemetry behavior is under test. Socket paths use the same short-path
`/tmp`-rooted fixture `tests/ipc/test_executor_server.py` uses, for the same `AF_UNIX` path-length
reason documented there.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kalshi_bot.ipc.poller_client import IPCPollerClient
from kalshi_bot.ipc.protocol import (
    SCHEMA_VERSION,
    WakeAck,
    WakeMessage,
    decode_wake_message,
    read_frame_body,
    write_frame,
)
from kalshi_bot.telemetry.db import TelemetryDB


def _make_message(**overrides: Any) -> WakeMessage:
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "correlation_id": "corr-1",
        "market_ticker": "KXTEST",
        "asset": "BTC",
        "direction": "yes",
        "kalshi_price": 0.5,
        "model_probability": 0.55,
        "fee": 0.01,
        "edge": 0.04,
        "decision_ts_ms": 1000,
        "sent_at_ms": 1000,
        "sent_at_ns": time.perf_counter_ns(),
    }
    base.update(overrides)
    return WakeMessage(**base)


@pytest.fixture
def telemetry_db(tmp_path: Path) -> TelemetryDB:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    return db


@pytest.fixture
def socket_path() -> Iterator[Path]:
    socket_dir = tempfile.mkdtemp(dir="/tmp")
    try:
        yield Path(socket_dir) / "executor.sock"
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


class _FakeExecutorServer:
    """A minimal stand-in executor: acks every message it receives, recording each."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.received: list[WakeMessage] = []
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))

    async def stop(self) -> None:
        # Close open connections before awaiting wait_closed(). On Python 3.12+,
        # Server.wait_closed() waits for every spawned connection handler to finish, and not only
        # for the listening socket, so a still-open connection deadlocks this otherwise. Same fix as
        # ExecutorServer.close() (see its docstring).
        for writer in self._writers:
            writer.close()
        self._writers = []
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            while True:
                try:
                    body = await read_frame_body(reader)
                except asyncio.IncompleteReadError:
                    break
                message = decode_wake_message(body)
                self.received.append(message)
                write_frame(
                    writer,
                    WakeAck(
                        schema_version=SCHEMA_VERSION,
                        correlation_id=message.correlation_id,
                        received_at_ms=0,
                        status="accepted",
                        reason=None,
                    ),
                )
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def _wait_until(predicate: Any, timeout_s: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


async def test_send_wake_delivers_message_once_connected(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    server = _FakeExecutorServer(socket_path)
    await server.start()
    client = IPCPollerClient(socket_path, telemetry_db)
    client.start()
    try:
        client.send_wake(_make_message())
        await _wait_until(lambda: len(server.received) == 1)
    finally:
        await client.close()
        await server.stop()
        telemetry_db.close()

    assert server.received[0].correlation_id == "corr-1"


async def test_send_wake_before_executor_is_up_is_delivered_after_it_starts(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    client = IPCPollerClient(socket_path, telemetry_db)
    client.start()  # no server listening yet
    client.send_wake(_make_message())
    await asyncio.sleep(0.05)  # let at least one failed connect attempt happen

    server = _FakeExecutorServer(socket_path)
    await server.start()
    try:
        await _wait_until(lambda: len(server.received) == 1)
    finally:
        await client.close()
        await server.stop()
        telemetry_db.close()


async def test_send_wake_never_blocks_when_queue_is_full(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    client = IPCPollerClient(socket_path, telemetry_db, queue_maxsize=2)
    # No server running, no start() called, so the queue just fills up. send_wake() must still
    # return.
    for i in range(5):
        client.send_wake(_make_message(correlation_id=f"corr-{i}"))
    assert client._dropped == 3
    telemetry_db.close()


async def test_reconnects_after_executor_restarts(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    server = _FakeExecutorServer(socket_path)
    await server.start()
    client = IPCPollerClient(socket_path, telemetry_db)
    client.start()
    server2: _FakeExecutorServer | None = None
    try:
        client.send_wake(_make_message(correlation_id="corr-before"))
        await _wait_until(lambda: len(server.received) == 1)

        await server.stop()
        await asyncio.sleep(0.05)
        server2 = _FakeExecutorServer(socket_path)
        await server2.start()
        client.send_wake(_make_message(correlation_id="corr-after"))
        await _wait_until(lambda: len(server2.received) == 1)
    finally:
        await client.close()
        if server2 is not None:
            await server2.stop()
        telemetry_db.close()

    assert server2 is not None
    assert server2.received[0].correlation_id == "corr-after"


async def test_wake_send_latency_event_is_recorded(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    server = _FakeExecutorServer(socket_path)
    await server.start()
    client = IPCPollerClient(socket_path, telemetry_db)
    client.start()
    try:
        client.send_wake(_make_message(correlation_id="corr-latency"))
        await _wait_until(lambda: len(server.received) == 1)
    finally:
        await client.close()
        await server.stop()
        telemetry_db.close()

    conn = sqlite3.connect(telemetry_db.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM latency_events "
            "WHERE stage = 'wake_send' AND correlation_id = 'corr-latency'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


async def test_close_before_start_does_not_raise(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    client = IPCPollerClient(socket_path, telemetry_db)
    await client.close()  # never started, so it must not raise
    telemetry_db.close()


async def test_shadow_wakes_are_dropped_before_the_queue_fills(tmp_path: Path) -> None:
    """A real fire must never queue behind a backlog of measurements.

    The case this guards is a reconnect: the client backs off for up to 30 seconds while shadow
    fires keep arriving, and `_write_loop` drains strictly in order. Without this, the next real
    fire waits behind every shadow wake queued during the outage.
    """
    telemetry_db = TelemetryDB(tmp_path / "telemetry.db")
    telemetry_db.initialize()
    # Never started, so nothing drains: the queue only grows, exactly as during a reconnect.
    client = IPCPollerClient(tmp_path / "nonexistent.sock", telemetry_db, queue_maxsize=8)
    try:
        for index in range(20):
            client.send_wake(_make_message(correlation_id=f"shadow-{index}", dry_run=True))
        queued_shadow = client._queue.qsize()

        client.send_wake(_make_message(correlation_id="real-fire", dry_run=False))
    finally:
        telemetry_db.close()

    # 25% of 8 is 2, so shadow fires stop being accepted at a depth of 2, far short of the
    # bound, leaving room for real fires.
    assert queued_shadow == 2
    assert client._queue.qsize() == 3

    drained = [client._queue.get_nowait() for _ in range(3)]
    assert [message.correlation_id for message in drained] == [
        "shadow-0",
        "shadow-1",
        "real-fire",
    ]


async def test_real_fires_still_fill_the_queue_to_its_bound(tmp_path: Path) -> None:
    """The shadow limit applies only to shadow fires, and real ones use the whole queue."""
    telemetry_db = TelemetryDB(tmp_path / "telemetry.db")
    telemetry_db.initialize()
    client = IPCPollerClient(tmp_path / "nonexistent.sock", telemetry_db, queue_maxsize=8)
    try:
        for index in range(20):
            client.send_wake(_make_message(correlation_id=f"real-{index}", dry_run=False))
    finally:
        telemetry_db.close()

    assert client._queue.qsize() == 8
