"""Tests for `ExecutorServer`.

Uses a fake `KalshiRestClient` wired through a real `OrderDispatcher` (same pattern
`tests/execution/test_order_dispatcher.py` uses) and a real `TelemetryDB` against a temp SQLite
file, with no live network. A real Unix domain socket is used, since this is the layer where the
socket itself is under test.

Socket paths come from a dedicated `socket_path` fixture rooted at `/tmp`, not pytest's
`tmp_path`. `tmp_path` nests deep enough (`.../pytest-of-<user>/pytest-N/<test-name>/...`) to
exceed `AF_UNIX`'s ~104-byte `sun_path` limit on macOS/BSD (108 on Linux), which was confirmed by
running this suite once against `tmp_path` directly and hitting `OSError: AF_UNIX path too long`,
exactly the risk the design research behind `docs/GUIDE.md` flagged.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import shutil
import sqlite3
import struct
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from kalshi_bot.execution.order_dispatcher import OrderDispatcher, permit_orders
from kalshi_bot.execution.position_sizing import BalanceCache
from kalshi_bot.execution.risk import RiskGate, RiskLimits
from kalshi_bot.ipc.executor_server import (
    DEFAULT_KELLY_FRACTION,
    DEFAULT_MAX_POSITION_PCT_OF_BALANCE,
    ExecutorServer,
    _snap_to_grid,
)
from kalshi_bot.ipc.protocol import (
    SCHEMA_VERSION,
    WakeMessage,
    decode_wake_ack,
    encode_frame,
    read_frame_body,
)
from kalshi_bot.runtime.killswitch import KillSwitch
from kalshi_bot.telemetry.db import TelemetryDB
from kalshi_bot.transport.rest_client import RequestTimings


class _FakeRestClient:
    def __init__(
        self, response: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self._response = response if response is not None else {}
        self._error = error

    async def post(
        self,
        path: str,
        body: dict[str, Any],
        timeout: aiohttp.ClientTimeout | None = None,
        timings: RequestTimings | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self.posted.append((path, body))
        if self._error is not None:
            raise self._error
        return self._response


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _make_message(**overrides: Any) -> WakeMessage:
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "correlation_id": "corr-1",
        "market_ticker": "KXTEST",
        "asset": "BTC",
        "direction": "yes",
        "kalshi_price": 0.42,
        "wire_price_yes_dollars": 0.42,
        "exchange_index": 2,
        "model_probability": 0.5,
        "fee": 0.01,
        "edge": 0.05,
        "decision_ts_ms": 1000,
        "sent_at_ms": 1001,
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


async def _wait_until(predicate: Any, timeout_s: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


class _RunningServer:
    def __init__(self, server: ExecutorServer, task: asyncio.Task[None]) -> None:
        self.server = server
        self.task = task


async def _start_server(server: ExecutorServer) -> _RunningServer:
    task = asyncio.create_task(server.serve_forever())
    await _wait_until(lambda: server._server is not None)
    return _RunningServer(server, task)


async def _stop_server(running: _RunningServer) -> None:
    await running.server.close()
    running.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await running.task


def _make_server(
    socket_path: Path,
    telemetry_db: TelemetryDB,
    rest_client: _FakeRestClient,
    *,
    fixed_order_contract_count: int = 1,
    kill_switch: KillSwitch | None = None,
    risk_gate: RiskGate | None = None,
    balance_cache: BalanceCache | None = None,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
    max_position_pct_of_balance: float = DEFAULT_MAX_POSITION_PCT_OF_BALANCE,
) -> ExecutorServer:
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]
    return ExecutorServer(
        socket_path=socket_path,
        dispatcher=dispatcher,
        telemetry_db=telemetry_db,
        fixed_order_contract_count=fixed_order_contract_count,
        order_time_in_force="fill_or_kill",
        order_self_trade_prevention_type="taker_at_cross",
        kill_switch=kill_switch,
        risk_gate=risk_gate,
        balance_cache=balance_cache,
        kelly_fraction=kelly_fraction,
        max_position_pct_of_balance=max_position_pct_of_balance,
    )


async def test_wake_message_is_acked_and_dispatched(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    rest_client = _FakeRestClient(response={"order_id": "ko-1"})
    server = _make_server(socket_path, telemetry_db, rest_client, fixed_order_contract_count=3)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(encode_frame(_make_message()))
        await writer.drain()

        ack = decode_wake_ack(await read_frame_body(reader))
        assert ack.status == "accepted"
        assert ack.correlation_id == "corr-1"

        await _wait_until(lambda: len(rest_client.posted) == 1)

        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    path, body = rest_client.posted[0]
    assert path == "/portfolio/events/orders"
    assert body["ticker"] == "KXTEST"
    assert body["side"] == "bid"
    assert body["count"] == "3.00"
    assert body["price"] == "0.4200"
    assert body["time_in_force"] == "fill_or_kill"
    assert body["self_trade_prevention_type"] == "taker_at_cross"
    # Routed explicitly from the wake's exchange_index, since auto-routing costs documented latency.
    assert body["exchange_index"] == 2

    conn = _connect(telemetry_db.db_path)
    try:
        order_row = conn.execute("SELECT * FROM orders_fired").fetchone()
        wake_recv_row = conn.execute(
            "SELECT * FROM latency_events WHERE stage = 'wake_recv'"
        ).fetchone()
    finally:
        conn.close()
    assert order_row["correlation_id"] == "corr-1"
    assert wake_recv_row["correlation_id"] == "corr-1"


async def _dispatch_one(
    socket_path: Path,
    telemetry_db: TelemetryDB,
    rest_client: _FakeRestClient,
    message: WakeMessage,
) -> None:
    server = _make_server(socket_path, telemetry_db, rest_client)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(encode_frame(message))
        await writer.drain()
        await read_frame_body(reader)  # drain the ack
        await _wait_until(lambda: len(rest_client.posted) == 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()


async def test_no_side_message_sends_ask_at_the_yes_bid_low_bid(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """A "no" fire sells YES *to the resting yes-bid*, never at the `1 - yes_bid` complement.

    `yes_bid = 0.30`: the poller's edge math ran against `kalshi_price = 0.70` (the no-side
    cost), but the wire order is `side=ask, price=0.3000`. Sending `0.7000` here is the
    silently-missed-edge half of the the YES-side wire-price rule bug. Under FOK it is killed
    instantly and looks like an ordinary no-fill.
    """
    rest_client = _FakeRestClient()
    await _dispatch_one(
        socket_path,
        telemetry_db,
        rest_client,
        _make_message(
            direction="no",
            correlation_id="corr-no-low",
            kalshi_price=0.70,
            wire_price_yes_dollars=0.30,
        ),
    )

    _, body = rest_client.posted[0]
    assert body["side"] == "ask"
    assert body["price"] == "0.3000"


async def test_no_side_message_sends_ask_at_the_yes_bid_high_bid(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """The money-losing direction of the YES-side wire-price rule: `yes_bid = 0.70` must go on the
    wire as `0.7000`.

    The pre-fix path sent the edge-math complement (`1 - 0.70 = 0.3000`), an offer to sell YES
    forty cents below the standing bid, which fills immediately at a guaranteed loss. This case,
    not the low-bid one, is the reason the fix exists.
    """
    rest_client = _FakeRestClient()
    await _dispatch_one(
        socket_path,
        telemetry_db,
        rest_client,
        _make_message(
            direction="no",
            correlation_id="corr-no-high",
            kalshi_price=0.30,
            wire_price_yes_dollars=0.70,
        ),
    )

    _, body = rest_client.posted[0]
    assert body["side"] == "ask"
    assert body["price"] == "0.7000"


async def test_yes_side_message_sends_bid_at_the_wire_price(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """A "yes" fire buys YES at the ask; wire price and edge-math price coincide."""
    rest_client = _FakeRestClient()
    await _dispatch_one(
        socket_path,
        telemetry_db,
        rest_client,
        _make_message(
            direction="yes",
            correlation_id="corr-yes",
            kalshi_price=0.55,
            wire_price_yes_dollars=0.55,
        ),
    )

    _, body = rest_client.posted[0]
    assert body["side"] == "bid"
    assert body["price"] == "0.5500"


async def test_real_fire_without_a_tradeable_wire_price_is_refused(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """A pre-v3 frame (wire price defaulted to `0.0`) must not dispatch at a made-up price.

    `0.0` is both the v2-frame default and what an empty book quotes; there is nothing correct
    to send, so the executor refuses the fire outright rather than falling back to
    `kalshi_price`, which for a "no" decision is the wrong-side price the YES-side wire-price rule
    records.
    """
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(
            encode_frame(
                _make_message(
                    direction="no",
                    correlation_id="corr-v2-frame",
                    kalshi_price=0.30,
                    wire_price_yes_dollars=0.0,
                )
            )
        )
        await writer.drain()
        ack = decode_wake_ack(await read_frame_body(reader))
        assert ack.status == "accepted"  # the frame itself is well-formed
        # Give the fire task a chance to run; the refusal must leave nothing posted.
        await asyncio.sleep(0.05)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    assert rest_client.posted == []


async def test_shadow_fire_without_a_tradeable_wire_price_still_runs_the_path(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """Shadow fires pass the wire-price guard, since refusing them would thin the latency sample.

    A `dry_run` wake never reaches the network, and demo books are mostly empty, so gating
    shadow fires on a tradeable price would silently restrict the `detect_fire` population to
    well-quoted books (the detect-to-fire measurement design). The path must still run to the
    pre-send stop.
    """
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(
            encode_frame(
                _make_message(
                    correlation_id="corr-shadow-empty",
                    kalshi_price=0.0,
                    wire_price_yes_dollars=0.0,
                    dry_run=True,
                )
            )
        )
        await writer.drain()
        await read_frame_body(reader)  # drain the ack
        # The dry_run dispatch still calls through to the rest client (the fake records it;
        # the real client stops before sending).
        await _wait_until(lambda: len(rest_client.posted) == 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()


async def test_multiple_messages_on_one_connection_are_all_dispatched(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        for i in range(5):
            writer.write(encode_frame(_make_message(correlation_id=f"corr-{i}")))
        await writer.drain()

        for _ in range(5):
            ack = decode_wake_ack(await read_frame_body(reader))
            assert ack.status == "accepted"

        await _wait_until(lambda: len(rest_client.posted) == 5)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    assert len(rest_client.posted) == 5


async def test_malformed_frame_is_acked_rejected_and_connection_stays_open(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        garbage = b"{not-json"
        writer.write(struct.pack(">I", len(garbage)) + garbage)
        await writer.drain()
        bad_ack = decode_wake_ack(await read_frame_body(reader))
        assert bad_ack.status == "rejected"

        # connection must still be usable for a well-formed message afterward
        writer.write(encode_frame(_make_message(correlation_id="corr-after-bad")))
        await writer.drain()
        good_ack = decode_wake_ack(await read_frame_body(reader))
        assert good_ack.status == "accepted"

        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()


async def test_dispatch_failure_does_not_crash_the_server(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    rest_client = _FakeRestClient(error=RuntimeError("boom"))
    server = _make_server(socket_path, telemetry_db, rest_client)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(encode_frame(_make_message()))
        await writer.drain()
        ack = decode_wake_ack(await read_frame_body(reader))
        assert ack.status == "accepted"  # ack precedes dispatch, so it's still accepted

        await _wait_until(lambda: len(rest_client.posted) == 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()


async def test_stale_socket_file_is_unlinked_before_bind(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.write_text("stale, from a prior crashed executor")

    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()


def test_order_build_is_recorded_but_not_written_before_dispatch() -> None:
    """`order_build` still lands, but no telemetry call sits between the build and the send.

    `record_latency_event()` raises on an unknown stage, takes a lock and may log synchronously
    when the queue is full, and makes the SQLite writer thread runnable. None of that belongs
    between deciding to place an order and placing it. A raise in particular would be caught by
    `_handle_fire`'s `except Exception` and logged as a dispatch failure while the order silently
    never went out. The duration is carried in a local and written afterwards instead.
    """
    source = inspect.getsource(ExecutorServer._handle_fire)
    build_index = source.index("build_template(")
    dispatch_index = source.index("self.dispatcher.dispatch(")
    assert "record_latency_event" not in source[build_index:dispatch_index]
    assert "self.telemetry_db" not in source[build_index:dispatch_index]


async def test_engaged_kill_switch_rejects_a_real_fire_with_an_audit_row(
    socket_path: Path, telemetry_db: TelemetryDB, tmp_path: Path
) -> None:
    """An engaged switch means no order goes out, and the refusal is visible instead of silent.

    The `rejected` row carries the wake's correlation_id, so an operator reading `orders_fired`
    after an incident sees exactly which decisions the halted bot declined to act on, joined to
    their decision rows. The plan's P2.1 acceptance check is precisely this pair: a rejected row
    and no order.
    """
    kill_switch = KillSwitch(tmp_path / "killswitch", ttl_seconds=0)
    kill_switch.engage("test halt")
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client, kill_switch=kill_switch)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(encode_frame(_make_message(correlation_id="corr-halted")))
        await writer.drain()
        ack = decode_wake_ack(await read_frame_body(reader))
        assert ack.status == "accepted"  # the frame is fine; the halt happens at dispatch
        await asyncio.sleep(0.05)  # give the fire task a chance to run
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    assert rest_client.posted == []

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()
    assert row["correlation_id"] == "corr-halted"
    assert row["status"] == "rejected"
    assert "kill switch engaged" in row["error_message"]
    assert "test halt" in row["error_message"]
    assert row["kalshi_order_id"] is None


async def test_released_kill_switch_lets_a_real_fire_through(
    socket_path: Path, telemetry_db: TelemetryDB, tmp_path: Path
) -> None:
    kill_switch = KillSwitch(tmp_path / "killswitch", ttl_seconds=0)
    rest_client = _FakeRestClient()
    await _dispatch_one(
        socket_path,
        telemetry_db,
        rest_client,
        _make_message(correlation_id="corr-live"),
    )

    assert len(rest_client.posted) == 1
    del kill_switch  # constructed to mirror the engaged test; never engaged


async def test_engaged_kill_switch_still_runs_shadow_fires(
    socket_path: Path, telemetry_db: TelemetryDB, tmp_path: Path
) -> None:
    """A halted bot keeps observing, including its latency measurement.

    Shadow fires place no order, so the switch has nothing to stop; blocking them would blind
    `detect_fire` exactly when an operator is watching the bot most closely.
    """
    kill_switch = KillSwitch(tmp_path / "killswitch", ttl_seconds=0)
    kill_switch.engage("test halt")
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client, kill_switch=kill_switch)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(encode_frame(_make_message(correlation_id="corr-shadow", dry_run=True)))
        await writer.drain()
        await read_frame_body(reader)  # drain the ack
        # The dry_run dispatch still reaches the rest client (the fake records it; the real
        # client stops before sending).
        await _wait_until(lambda: len(rest_client.posted) == 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        orders = conn.execute("SELECT COUNT(*) AS n FROM orders_fired").fetchone()["n"]
    finally:
        conn.close()
    assert orders == 0  # a shadow fire never writes an orders_fired row, halted or not


def _tight_risk_gate(**overrides: Any) -> RiskGate:
    base: dict[str, Any] = {
        "allowed_exchange_indexes": frozenset({2}),
        "refire_cooldown_seconds": 60.0,
    }
    base.update(overrides)
    return RiskGate(RiskLimits(**base))


async def test_risk_gate_rejection_writes_a_rejected_row_and_no_order(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """A wake targeting a disallowed shard is refused with an audit row, per the shard gate."""
    gate = _tight_risk_gate(allowed_exchange_indexes=frozenset({0}))  # wake carries 2
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client, risk_gate=gate)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(encode_frame(_make_message(correlation_id="corr-risk")))
        await writer.drain()
        await read_frame_body(reader)  # drain the ack
        await asyncio.sleep(0.05)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    assert rest_client.posted == []

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()
    assert row["correlation_id"] == "corr-risk"
    assert row["status"] == "rejected"
    assert row["error_message"].startswith("risk gate: ")
    assert "exchange_index=2" in row["error_message"]


async def test_risk_gate_cooldown_blocks_an_immediate_refire(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """Two identical wakes in quick succession: the first dispatches, the second is rejected."""
    gate = _tight_risk_gate()
    rest_client = _FakeRestClient(response={"order_id": "ko-1", "fill_count": "1.00"})
    server = _make_server(socket_path, telemetry_db, rest_client, risk_gate=gate)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(encode_frame(_make_message(correlation_id="corr-first")))
        await writer.drain()
        await read_frame_body(reader)
        await _wait_until(lambda: len(rest_client.posted) == 1)

        writer.write(encode_frame(_make_message(correlation_id="corr-refire")))
        await writer.drain()
        await read_frame_body(reader)
        await asyncio.sleep(0.05)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    assert len(rest_client.posted) == 1  # the re-fire never reached the wire

    conn = _connect(telemetry_db.db_path)
    try:
        rows = {
            row["correlation_id"]: row["status"]
            for row in conn.execute("SELECT correlation_id, status FROM orders_fired")
        }
    finally:
        conn.close()
    assert rows["corr-first"] == "filled"
    assert rows["corr-refire"] == "rejected"


async def test_risk_gate_correlation_group_cap_blocks_a_second_ticker(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """An hourly strike and its 15-minute counterpart never share a ticker, so only the group
    cap, and neither per-ticker cap, can see them as the same exposure."""
    gate = _tight_risk_gate(max_attempts_per_correlation_group_per_day=1)
    rest_client = _FakeRestClient(response={"order_id": "ko-1", "fill_count": "1.00"})
    server = _make_server(socket_path, telemetry_db, rest_client, risk_gate=gate)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(
            encode_frame(
                _make_message(
                    correlation_id="corr-hourly",
                    market_ticker="KXBTCD-T100",
                    correlation_group="majors",
                )
            )
        )
        await writer.drain()
        await read_frame_body(reader)
        await _wait_until(lambda: len(rest_client.posted) == 1)

        writer.write(
            encode_frame(
                _make_message(
                    correlation_id="corr-15m",
                    market_ticker="KXBTC15M-T100",
                    correlation_group="majors",
                )
            )
        )
        await writer.drain()
        await read_frame_body(reader)
        await asyncio.sleep(0.05)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    assert len(rest_client.posted) == 1  # the second, correlated ticker never reached the wire

    conn = _connect(telemetry_db.db_path)
    try:
        rows = {row["correlation_id"]: row for row in conn.execute("SELECT * FROM orders_fired")}
    finally:
        conn.close()
    assert rows["corr-hourly"]["status"] == "filled"
    assert rows["corr-hourly"]["correlation_group"] == "majors"
    assert rows["corr-15m"]["status"] == "rejected"
    assert "majors" in rows["corr-15m"]["error_message"]


async def test_shadow_fires_do_not_consume_risk_budget(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """A shadow fire must not start cooldowns or count as an attempt."""
    gate = _tight_risk_gate()
    rest_client = _FakeRestClient(response={"order_id": "ko-1", "fill_count": "1.00"})
    server = _make_server(socket_path, telemetry_db, rest_client, risk_gate=gate)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(encode_frame(_make_message(correlation_id="corr-shadow", dry_run=True)))
        await writer.drain()
        await read_frame_body(reader)
        await _wait_until(lambda: len(rest_client.posted) == 1)

        # A real fire right after the shadow: no cooldown may have started.
        writer.write(encode_frame(_make_message(correlation_id="corr-real")))
        await writer.drain()
        await read_frame_body(reader)
        await _wait_until(lambda: len(rest_client.posted) == 2)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()


async def test_count_is_capped_to_available_liquidity(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """A FOK for more contracts than rest is a guaranteed kill; size to what can fill."""
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client, fixed_order_contract_count=5)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(
            encode_frame(_make_message(correlation_id="corr-sized", available_size_contracts=2.0))
        )
        await writer.drain()
        await read_frame_body(reader)
        await _wait_until(lambda: len(rest_client.posted) == 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["count"] == "2.00"


async def test_unknown_liquidity_keeps_the_configured_count(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """`available_size_contracts == 0.0` means unknown (pre-v3 frame), not empty."""
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client, fixed_order_contract_count=5)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(
            encode_frame(_make_message(correlation_id="corr-unknown", available_size_contracts=0.0))
        )
        await writer.drain()
        await read_frame_body(reader)
        await _wait_until(lambda: len(rest_client.posted) == 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["count"] == "5.00"


async def test_real_fire_sizes_off_kelly_when_a_balance_cache_is_configured(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """edge=0.05, kalshi_price=0.42 (the `_make_message` defaults), balance=$1000, default
    kelly_fraction=0.15: full Kelly is 0.05/0.58 ~= 0.08621, scaled to ~0.01293 of balance,
    $12.93 staked at $0.42/contract -> 30 contracts, not the fixed floor of 1."""
    rest_client = _FakeRestClient()
    cache = BalanceCache(balance_dollars=1000.0)
    server = _make_server(socket_path, telemetry_db, rest_client, balance_cache=cache)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(encode_frame(_make_message(correlation_id="corr-kelly")))
        await writer.drain()
        await read_frame_body(reader)
        await _wait_until(lambda: len(rest_client.posted) == 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["count"] == "30.00"


async def test_kelly_sized_count_still_clamps_to_available_liquidity(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """The same fire as above, but the book only rests 10 contracts. Kelly wants 30, and the
    resting-liquidity cap still wins."""
    rest_client = _FakeRestClient()
    cache = BalanceCache(balance_dollars=1000.0)
    server = _make_server(socket_path, telemetry_db, rest_client, balance_cache=cache)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(
            encode_frame(
                _make_message(correlation_id="corr-kelly-capped", available_size_contracts=10.0)
            )
        )
        await writer.drain()
        await read_frame_body(reader)
        await _wait_until(lambda: len(rest_client.posted) == 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["count"] == "10.00"


async def test_max_position_pct_of_balance_caps_a_large_edge(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """A big enough edge would stake far more than the configured ceiling allows."""
    rest_client = _FakeRestClient()
    cache = BalanceCache(balance_dollars=1000.0)
    server = _make_server(
        socket_path,
        telemetry_db,
        rest_client,
        balance_cache=cache,
        kelly_fraction=1.0,
        max_position_pct_of_balance=0.02,
    )
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(
            encode_frame(
                _make_message(correlation_id="corr-ceiling", edge=0.90, kalshi_price=0.10, fee=0.01)
            )
        )
        await writer.drain()
        await read_frame_body(reader)
        await _wait_until(lambda: len(rest_client.posted) == 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    # 2% of $1000 = $20 at $0.10/contract = 200 contracts, regardless of how large edge is.
    _, body = rest_client.posted[0]
    assert body["count"] == "200.00"


async def test_shadow_fire_ignores_the_balance_cache_and_uses_the_fixed_count(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """A shadow fire's whole point is a size-independent, identical timed path, so it must not
    start sizing off edge and balance just because a real fire would."""
    rest_client = _FakeRestClient()
    cache = BalanceCache(balance_dollars=1_000_000.0)  # would size far above the fixed count
    server = _make_server(
        socket_path, telemetry_db, rest_client, fixed_order_contract_count=3, balance_cache=cache
    )
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(encode_frame(_make_message(correlation_id="corr-shadow-kelly", dry_run=True)))
        await writer.drain()
        await read_frame_body(reader)
        await _wait_until(lambda: len(rest_client.posted) == 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["count"] == "3.00"


async def test_no_balance_cache_falls_back_to_the_fixed_count(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """An unconfigured `balance_cache=None` (this deployment's behavior before sizing existed,
    and every other test in this file) must not crash and must not size a real fire off nothing."""
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client, fixed_order_contract_count=4)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(encode_frame(_make_message(correlation_id="corr-no-cache")))
        await writer.drain()
        await read_frame_body(reader)
        await _wait_until(lambda: len(rest_client.posted) == 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["count"] == "4.00"


async def test_sub_contract_liquidity_refuses_the_real_fire(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """Size known and under one contract, so no legal order could fill. Refuse with a row."""
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client)
    running = await _start_server(server)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        writer.write(
            encode_frame(_make_message(correlation_id="corr-thin", available_size_contracts=0.5))
        )
        await writer.drain()
        await read_frame_body(reader)
        await asyncio.sleep(0.05)
        writer.close()
        await writer.wait_closed()
    finally:
        await _stop_server(running)
        telemetry_db.close()

    assert rest_client.posted == []

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()
    assert row["status"] == "rejected"
    assert "insufficient resting volume" in row["error_message"]


async def test_socket_is_owner_only(socket_path: Path, telemetry_db: TelemetryDB) -> None:
    """Anything that can connect to this socket can place real orders; 0600 limits it to us."""
    rest_client = _FakeRestClient()
    server = _make_server(socket_path, telemetry_db, rest_client)
    running = await _start_server(server)
    try:
        mode = socket_path.stat().st_mode & 0o777
    finally:
        await _stop_server(running)
        telemetry_db.close()

    assert mode == 0o600


async def test_wire_price_is_snapped_to_the_markets_grid(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """Off-grid prices are rejected by Kalshi outright; the executor snaps to the market's step.

    The step rides the wake from the ladder's `price_ranges` read, so the grid is consumed
    dynamically, not assumed to be cents (the docs warn new structures get introduced).
    """
    rest_client = _FakeRestClient()
    await _dispatch_one(
        socket_path,
        telemetry_db,
        rest_client,
        _make_message(
            correlation_id="corr-grid",
            kalshi_price=0.42,
            wire_price_yes_dollars=0.42,
            price_ranges=[[0.0, 1.0, 0.05]],
        ),
    )

    _, body = rest_client.posted[0]
    assert body["price"] == "0.4000"  # nearest multiple of 0.05


async def test_cent_grid_leaves_quoted_prices_untouched(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """On today's universal linear_cent grid the snap is a no-op for Kalshi-quoted prices."""
    rest_client = _FakeRestClient()
    await _dispatch_one(
        socket_path,
        telemetry_db,
        rest_client,
        _make_message(correlation_id="corr-cent", wire_price_yes_dollars=0.96),
    )

    _, body = rest_client.posted[0]
    assert body["price"] == "0.9600"


_TAPERED_RANGES = [[0.0, 0.1, 0.001], [0.1, 0.9, 0.01], [0.9, 1.0, 0.001]]


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (0.0523, 0.052),  # left tail: 0.1-cent step
        (0.42, 0.42),  # middle: 1-cent step, already on-grid
        (0.4237, 0.42),  # middle: snapped down to nearest cent
        (0.9531, 0.953),  # right tail: 0.1-cent step
        (0.1, 0.1),  # boundary shared by two ranges: either step divides it evenly
        (0.9, 0.9),  # boundary shared by two ranges: either step divides it evenly
    ],
)
def test_snap_to_grid_is_range_aware(price: float, expected: float) -> None:
    """The 15-minute crypto series' tapered grid must snap to whichever range a price falls in."""
    assert _snap_to_grid(price, _TAPERED_RANGES) == pytest.approx(expected)


def test_snap_to_grid_falls_back_to_one_cent_outside_every_range() -> None:
    assert _snap_to_grid(1.567, _TAPERED_RANGES) == pytest.approx(1.57)
    assert _snap_to_grid(0.437, []) == pytest.approx(0.44)


async def test_wire_price_is_snapped_to_a_tapered_grid(
    socket_path: Path, telemetry_db: TelemetryDB
) -> None:
    """End-to-end: a tail-zone price is snapped to its tenth-of-a-cent step, not the mid step."""
    rest_client = _FakeRestClient()
    await _dispatch_one(
        socket_path,
        telemetry_db,
        rest_client,
        _make_message(
            correlation_id="corr-tapered",
            kalshi_price=0.0523,
            wire_price_yes_dollars=0.0523,
            price_ranges=_TAPERED_RANGES,
        ),
    )

    _, body = rest_client.posted[0]
    assert body["price"] == "0.0520"
