"""One end-to-end pass over the real IPC path: real client, socket, server and dispatcher.

Every other test in `tests/ipc/` drives one side against a stub of the other. That is the right
way to pin each side's contract and the wrong way to catch failures that live *between* them:
a framing mismatch, a field the encoder writes and the decoder ignores, an acknowledgement
ordering that only matters when both halves are real. Those are the bugs this file exists for.

The only fake is the venue: `_FakeRestClient` stands in for the exchange, because a test that
reaches a live API is forbidden here. Everything up to that boundary is the shipping code,
including the length-prefixed framing, the Unix domain socket, and the dispatcher's permission
guard.

This replaces the integration test that lived here before the extraction, which drove the same
path from the decision pipeline. That package is not part of this repository, so the wake is
constructed directly instead; what is covered is the same span from `send_wake` to an order
reaching the venue client.
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

from kalshi_bot.execution.order_dispatcher import OrderDispatcher, permit_orders
from kalshi_bot.ipc.executor_server import ExecutorServer
from kalshi_bot.ipc.poller_client import IPCPollerClient
from kalshi_bot.ipc.protocol import SCHEMA_VERSION, WakeMessage
from kalshi_bot.telemetry.db import TelemetryDB


class _FakeRestClient:
    """Stands in for the exchange. Records every order body it is handed.

    It accepts arbitrary keyword arguments on purpose: `OrderDispatcher.dispatch()` passes
    `timeout`, `timings` and `dry_run` today and may pass more later, and a fake that falls
    behind that signature fails with a `TypeError` the dispatcher catches and logs, which
    presents as "no order arrived" with nothing pointing here.
    """

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    async def post(self, path: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.posted.append(body)
        return {
            "order_id": f"test-order-{len(self.posted)}",
            "fill_count": "1.00",
            "remaining_count": "0.00",
        }


@pytest.fixture
def socket_path() -> Iterator[Path]:
    """A socket path short enough for `AF_UNIX`.

    `sun_path` is capped at around 100 bytes, and pytest's `tmp_path` embeds the test's own name
    so a descriptively named test binds a path the kernel refuses, and the failure (`OSError:
    AF_UNIX path too long`) names the socket instead of the test name that produced it. Rooting
    at `/tmp` keeps the path short regardless of how the test is called.
    """
    socket_dir = tempfile.mkdtemp(dir="/tmp")
    try:
        yield Path(socket_dir) / "executor.sock"
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


async def _wait_until(predicate: object, timeout_s: float = 5.0) -> None:
    """Poll `predicate` until it is true or the deadline passes.

    Args:
        predicate: A zero-argument callable returning a truthiness.
        timeout_s: How long to wait before giving up.

    Raises:
        TimeoutError: If the deadline passes first.
    """
    assert callable(predicate)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("condition not met before timeout")
        await asyncio.sleep(0.005)


def _wake(correlation_id: str) -> WakeMessage:
    """A well-formed wake for a market the fake venue will accept."""
    return WakeMessage(
        schema_version=SCHEMA_VERSION,
        correlation_id=correlation_id,
        market_ticker="KXTEST-T100",
        asset="TEST",
        direction="yes",
        kalshi_price=0.5,
        wire_price_yes_dollars=0.5,
        model_probability=0.55,
        fee=0.01,
        edge=0.04,
        decision_ts_ms=0,
        sent_at_ms=int(time.time() * 1000),
        sent_at_ns=time.perf_counter_ns(),
    )


@pytest.mark.asyncio
async def test_a_wake_crosses_the_socket_and_becomes_an_order(
    tmp_path: Path, socket_path: Path
) -> None:
    """The whole span: `send_wake` on the poller side, an order body on the venue side."""
    poller_db = TelemetryDB(tmp_path / "telemetry.db")
    poller_db.initialize()
    executor_db = TelemetryDB(tmp_path / "telemetry.db")
    executor_db.initialize()

    venue = _FakeRestClient()
    dispatcher = OrderDispatcher(venue, executor_db, permit_orders)  # type: ignore[arg-type]
    server = ExecutorServer(
        socket_path=socket_path,
        dispatcher=dispatcher,
        telemetry_db=executor_db,
        fixed_order_contract_count=1,
        order_time_in_force="fill_or_kill",
        order_self_trade_prevention_type="taker_at_cross",
        kill_switch=None,
        risk_gate=None,
        balance_cache=None,
    )
    server_task = asyncio.create_task(server.serve_forever())
    client = IPCPollerClient(socket_path, poller_db)
    try:
        await _wait_until(socket_path.exists)
        client.start()

        client.send_wake(_wake("round-trip-1"))

        await _wait_until(lambda: len(venue.posted) >= 1)
        body = venue.posted[0]
        assert body["ticker"] == "KXTEST-T100"
        # The wake carries an *outcome* ("yes"); the wire wants a *book side* ("bid"). Asserting
        # the translated value rather than the one the wake carried is the point: a body that
        # echoed "yes" straight through would be well-formed, would post without error, and would
        # be the wrong order. Two well-formed halves, meaningless only combined.
        assert body["side"] == "bid"
        assert body["count"] == "1.00"
        # Both quantities cross the wire as fixed-precision strings, not numbers. Asserting
        # the string is deliberate: a float here would silently accept a formatting change
        # that the venue would reject.
        assert body["price"] == "0.5000"
    finally:
        await client.close()
        await server.close()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        poller_db.close()
        executor_db.close()


@pytest.mark.asyncio
async def test_both_ends_record_the_same_correlation_id(tmp_path: Path, socket_path: Path) -> None:
    """The correlation id is what joins a poller row to an executor row after the fact.

    Both processes write to the same telemetry database and neither can see the other's rows at
    write time, so the join key crossing the socket intact is the only thing making a fire
    reconstructable later. A framing or encoding change that dropped it would leave both sides
    individually plausible and the audit trail useless, the shape of bug this file is for.
    """
    db_path = tmp_path / "telemetry.db"
    poller_db = TelemetryDB(db_path)
    poller_db.initialize()
    executor_db = TelemetryDB(db_path)
    executor_db.initialize()

    venue = _FakeRestClient()
    dispatcher = OrderDispatcher(venue, executor_db, permit_orders)  # type: ignore[arg-type]
    server = ExecutorServer(
        socket_path=socket_path,
        dispatcher=dispatcher,
        telemetry_db=executor_db,
        fixed_order_contract_count=1,
        order_time_in_force="fill_or_kill",
        order_self_trade_prevention_type="taker_at_cross",
        kill_switch=None,
        risk_gate=None,
        balance_cache=None,
    )
    server_task = asyncio.create_task(server.serve_forever())
    client = IPCPollerClient(socket_path, poller_db)
    try:
        await _wait_until(socket_path.exists)
        client.start()
        client.send_wake(_wake("shared-correlation"))
        await _wait_until(lambda: len(venue.posted) >= 1)
    finally:
        await client.close()
        await server.close()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        poller_db.close()
        executor_db.close()

    conn = sqlite3.connect(db_path)
    try:
        stages = {
            row[0]
            for row in conn.execute(
                "SELECT stage FROM latency_events WHERE correlation_id = ?",
                ("shared-correlation",),
            )
        }
        orders = conn.execute(
            "SELECT COUNT(*) FROM orders_fired WHERE correlation_id = ?",
            ("shared-correlation",),
        ).fetchone()[0]
    finally:
        conn.close()

    assert {"wake_send", "wake_recv"} <= stages
    assert orders == 1
