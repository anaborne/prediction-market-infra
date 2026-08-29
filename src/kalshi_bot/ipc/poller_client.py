"""Poller-side IPC client.

Owns the outbound connection to the executor's Unix domain socket. `send_wake()` never blocks
the caller (`decision.runner.AssetPipeline._record()`'s hot path). It enqueues onto an internal
queue and returns immediately. A background task, started by `start()`, owns the actual
connection: it drains the queue, writes frames, reads back acks for logging, and reconnects with
backoff on any failure. See `docs/GUIDE.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from pathlib import Path

from kalshi_bot.ipc.protocol import WakeMessage, decode_wake_ack, read_frame_body, write_frame
from kalshi_bot.telemetry.db import TelemetryDB

# Pre-serialized; the identical constant lives in `ipc/executor_server.py` and
# `decision/runner.py`. Every `latency_events` row carries it so that filtering the table by
# population uses one consistent denominator across all stages.
_DRY_RUN_METADATA = {False: '{"dry_run": false}', True: '{"dry_run": true}'}

logger = logging.getLogger(__name__)

_DROP_LOG_SAMPLE_EVERY = 20
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0

# Fraction of the queue a shadow fire is allowed to occupy. Past this depth, shadow wakes are
# dropped so a real fire never queues behind a backlog of measurements.
#
# The case this exists for is a reconnect: `_run` backs off up to 30 seconds, shadow wakes keep
# arriving, and `_write_loop` drains strictly in order, so a real fire arriving after the
# reconnect would wait behind every shadow wake queued during the outage, each one a frame write
# plus a drain. Dropping measurements under backpressure is free; delaying an order is not.
_SHADOW_QUEUE_FRACTION = 0.25


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class IPCPollerClient:
    """Non-blocking client for sending `WakeMessage`s to the executor process.

    A background task (started by `start()`) owns the connection lifecycle: connect, drain the
    outbound queue, read back acks for logging, and reconnect with exponential backoff + full
    jitter on any failure, retrying forever, since a wake channel that gives up permanently
    would silently stop firing for the rest of the process's life. This is a new, independent
    implementation rather than a reuse of `ingest/reconnect.py::reconnecting_stream()`, which is
    shaped for inbound async-generator streams, not an outbound persistent writer.

    If the executor is unreachable (or has been slow to drain) long enough that the internal
    queue fills, `send_wake()` drops the new message with a rate-limited log line, the same drop
    policy as `AssetPipeline.offer()`, instead of blocking the caller or growing the queue
    unbounded. Shadow fires are dropped earlier and more readily than real ones; see
    `send_wake()`.

    Attributes:
        socket_path: Filesystem path of the executor's Unix domain socket.
        telemetry_db: Sink for this process's own fire-and-forget `latency_events` writes
            (`'wake_send'`), the poller's existing `TelemetryDB` instance, per the poller/executor
            IPC design.
    """

    def __init__(
        self, socket_path: Path, telemetry_db: TelemetryDB, queue_maxsize: int = 1000
    ) -> None:
        self.socket_path = socket_path
        self.telemetry_db = telemetry_db
        self._queue: asyncio.Queue[WakeMessage] = asyncio.Queue(queue_maxsize)
        self._task: asyncio.Task[None] | None = None
        self._dropped = 0
        self._dropped_shadow = 0
        self._shadow_queue_limit = max(1, int(queue_maxsize * _SHADOW_QUEUE_FRACTION))

    def send_wake(self, message: WakeMessage) -> None:
        """Enqueue `message` for sending. Never blocks; drops on a full queue.

        Shadow fires (`message.dry_run`) yield to real ones: they are dropped once the queue is
        deeper than `_SHADOW_QUEUE_FRACTION` of its bound, well before it is full. A dropped
        measurement costs one sample out of thousands; a real fire stuck behind a queue of
        measurements costs the edge it was placed for.

        Args:
            message: The wake to send. `message.sent_at_ms` should already be set by the caller
                (the moment the fire decision was made to enqueue), so `'wake_send'`'s recorded
                duration includes any time spent waiting in this queue as well as the socket
                write itself.
        """
        if message.dry_run and self._queue.qsize() >= self._shadow_queue_limit:
            self._dropped_shadow += 1
            if self._dropped_shadow % _DROP_LOG_SAMPLE_EVERY == 1:
                logger.info(
                    "wake queue at %d, dropping shadow fires to keep the order path clear "
                    "(%d dropped total)",
                    self._queue.qsize(),
                    self._dropped_shadow,
                )
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % _DROP_LOG_SAMPLE_EVERY == 1:
                logger.warning(
                    "wake queue full, dropped message for %s (%d dropped total)",
                    message.market_ticker,
                    self._dropped,
                )

    def start(self) -> None:
        """Start the background connect/send/reconnect loop. Idempotent; call once."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        """Cancel the background loop and wait for it to finish. Idempotent."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        backoff = _INITIAL_BACKOFF_S
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(path=str(self.socket_path))
            except OSError:
                logger.warning(
                    "executor unreachable at %s, retrying in %.1fs", self.socket_path, backoff
                )
                await asyncio.sleep(backoff * (0.5 + random.random()))
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
                continue

            backoff = _INITIAL_BACKOFF_S
            logger.info("connected to executor at %s", self.socket_path)
            await self._run_connection(reader, writer)
            logger.warning("lost connection to executor at %s, reconnecting", self.socket_path)

    async def _run_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        write_task = asyncio.create_task(self._write_loop(writer))
        read_task = asyncio.create_task(self._read_ack_loop(reader))
        try:
            await asyncio.wait({write_task, read_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (write_task, read_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(write_task, read_task, return_exceptions=True)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _write_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            message = await self._queue.get()
            write_frame(writer, message)
            await writer.drain()
            ended_at_ms = _now_ms()
            # started_at_ms/ended_at_ms stay millisecond wall-clock (cross-referenceable against
            # other rows); duration_ms is computed from perf_counter_ns instead of their
            # difference, since ms resolution can't itself confirm/refute the <1ms wake_send
            # budget. See WakeMessage.sent_at_ns's docstring and the poller/executor IPC design's
            # Consequences.
            duration_ms = (time.perf_counter_ns() - message.sent_at_ns) / 1_000_000
            self.telemetry_db.record_latency_event(
                {
                    "correlation_id": message.correlation_id,
                    "stage": "wake_send",
                    "started_at_ms": message.sent_at_ms,
                    "ended_at_ms": ended_at_ms,
                    "duration_ms": duration_ms,
                    "metadata_json": _DRY_RUN_METADATA[message.dry_run],
                }
            )

    async def _read_ack_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            body = await read_frame_body(reader)
            ack = decode_wake_ack(body)
            if ack.status != "accepted":
                logger.warning("executor rejected wake %s: %s", ack.correlation_id, ack.reason)
