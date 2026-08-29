"""Executor-side IPC server.

Listens on a Unix domain socket for `WakeMessage`s from the poller process and, for each one,
builds a `PrebuiltOrderTemplate` and calls `OrderDispatcher.dispatch()` for real. Owns zero
decision/strategy knowledge. Everything needed to act on a fire (which ticker, which side, what
price) comes off the wire, and everything about *how* to execute (contract count, time in force,
self-trade prevention) is this process's own config, per the poller/executor IPC design
(`docs/GUIDE.md`). Contract count for a real fire is sized by
`execution.position_sizing.size_position()` off `edge`/`kalshi_price` (both already on the
wire for logging before sizing existed) and the executor's own periodically-polled balance. This
is still "how to execute" and not "what to trade": the module has no dependency on `decision` and
does not evaluate whether a fire is a good idea, only how much to stake on one it already received.

Acks a `WakeMessage` immediately after it's read and decoded, before `dispatch()` is awaited, so
Kalshi's network round trip never inflates the `wake_send`/`wake_recv` latency measurement. Each
accepted fire is handled in its own `asyncio.create_task()` so one slow `dispatch()` call never
blocks reading the next wake message on the same (or another) connection.

This process is also where the `detect_fire` measurement is closed. The poller stamps `recv_ns`
when a WebSocket frame finishes parsing and carries it on the wake; here it is subtracted from the
moment the order is handed to `aiohttp`. The subtraction crosses a process boundary, so it is
guarded by `WakeMessage.is_clock_comparable_to()`, since two monotonic counters from different
hosts or different boots produce no error, only a wrong number. See
`docs/GUIDE.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from pathlib import Path

from kalshi_bot.config import OrderSelfTradePreventionType, OrderTimeInForce
from kalshi_bot.execution.order_dispatcher import OrderDispatcher
from kalshi_bot.execution.position_sizing import BalanceCache, size_position
from kalshi_bot.execution.prebuilt_orders import build_template
from kalshi_bot.execution.risk import RiskGate
from kalshi_bot.ipc.protocol import (
    SCHEMA_VERSION,
    WakeAck,
    WakeMessage,
    decode_wake_message,
    read_frame_body,
    write_frame,
)
from kalshi_bot.runtime.clock import clock_domain, monotonic_ns
from kalshi_bot.runtime.killswitch import KillSwitch
from kalshi_bot.telemetry.db import TelemetryDB
from kalshi_bot.transport.rest_client import RequestTimings

logger = logging.getLogger(__name__)


# Pre-serialized, because it is written on every latency row on the hot path and there are
# exactly two possible values. Keyed by `WakeMessage.dry_run`.
_DRY_RUN_METADATA = {False: '{"dry_run": false}', True: '{"dry_run": true}'}

# Deliberately conservative: a strategy still being tuned should start staking a small slice of
# full Kelly at a tight ceiling, not the other way around. See
# `execution.position_sizing`'s module docstring and `docs/configuration.md`'s "Position sizing"
# section for how to raise these as confidence in the model grows.
DEFAULT_KELLY_FRACTION = 0.15
DEFAULT_MAX_POSITION_PCT_OF_BALANCE = 0.02


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _snap_to_grid(price: float, ranges: list[list[float]]) -> float:
    """Round `price` to the nearest multiple of whichever range's step it falls in.

    `ranges` is `WakeMessage.price_ranges`: `[start, end, step]` triples, most markets carrying
    exactly one (`[0.0, 1.0, 0.01]`) but the 15-minute crypto series carrying three, tighter in
    the tails (`docs/GUIDE.md`, `ingest.strike_ladder._price_ranges_of`). The matching range
    is the one whose `[start, end]` contains `price`; a price exactly on a boundary shared by two
    ranges matches whichever is checked first, which is fine since both ranges' steps evenly
    divide that boundary. No match (an empty `ranges`, or a price outside every range, which is
    garbage on the wire) falls back to one cent, the coarsest grid on this exchange.
    """
    for start, end, step in ranges:
        if step > 0 and start <= price <= end:
            return round(price / step) * step
    return round(price / 0.01) * 0.01


class ExecutorServer:
    """Owns the Unix domain socket listener and per-fire order dispatch.

    Attributes:
        socket_path: Filesystem path of the Unix domain socket to listen on. Any stale socket
            file left behind by a prior (e.g. crashed) executor is unlinked before binding.
        dispatcher: Used to fire real orders.
        telemetry_db: Sink for this process's own fire-and-forget `latency_events` writes
            (`'wake_recv'`), a separate `TelemetryDB` instance from the poller's, per the
            poller/executor IPC design.
        fixed_order_contract_count: Contract count used for every shadow fire, and the floor
            under every real fire's `execution.position_sizing.size_position()` result. Was the
            only size any fire ever used before position sizing existed. See the poller/executor
            IPC design.
        balance_cache: Refreshed on an interval by `account_monitor.poll_account_periodically`;
            read (never fetched) by `size_position()` at fire time. `None` disables real sizing
            entirely, so every real fire uses `fixed_order_contract_count` as if it were still
            the only size, the pre-sizing-model behavior.
        kelly_fraction: Fraction of full Kelly to stake on a real fire. See
            `execution.position_sizing`'s module docstring for why this should start low.
        max_position_pct_of_balance: Hard ceiling on the staked fraction of `balance_cache`,
            applied after `kelly_fraction`.
        order_time_in_force: `time_in_force` used for every real fire.
        order_self_trade_prevention_type: `self_trade_prevention_type` used for every real fire.
        kill_switch: Checked before every real dispatch; while engaged, a real fire is refused
            and recorded as a `rejected` row instead of an order. `None` disables the check.
            Every production entry point passes one, and the parameter is keyword-required so a
            construction site cannot simply forget the decision. Shadow fires bypass the switch:
            they place no order, and halting them would blind the latency measurement exactly
            when an operator is watching the bot most closely.
        risk_gate: The last check before dispatch (`execution/risk.py`): attempt, notional,
            position, concurrency, cooldown, and shard-allowlist caps, all pure in-memory
            arithmetic at fire time. A rejection is recorded as a `rejected` row. `None`
            disables it; keyword-required for the same reason as `kill_switch`. Shadow fires
            bypass it, because they place no order and must not consume attempt budget.
    """

    def __init__(
        self,
        socket_path: Path,
        dispatcher: OrderDispatcher,
        telemetry_db: TelemetryDB,
        fixed_order_contract_count: int,
        order_time_in_force: OrderTimeInForce,
        order_self_trade_prevention_type: OrderSelfTradePreventionType,
        *,
        kill_switch: KillSwitch | None,
        risk_gate: RiskGate | None,
        balance_cache: BalanceCache | None,
        kelly_fraction: float = DEFAULT_KELLY_FRACTION,
        max_position_pct_of_balance: float = DEFAULT_MAX_POSITION_PCT_OF_BALANCE,
    ) -> None:
        self.socket_path = socket_path
        self.dispatcher = dispatcher
        self.telemetry_db = telemetry_db
        self.fixed_order_contract_count = fixed_order_contract_count
        self.order_time_in_force = order_time_in_force
        self.order_self_trade_prevention_type = order_self_trade_prevention_type
        self.kill_switch = kill_switch
        self.risk_gate = risk_gate
        self.balance_cache = balance_cache
        self.kelly_fraction = kelly_fraction
        self.max_position_pct_of_balance = max_position_pct_of_balance
        self._server: asyncio.Server | None = None
        self._fire_tasks: set[asyncio.Task[None]] = set()
        self._active_writers: set[asyncio.StreamWriter] = set()
        # Read once at construction rather than per fire: it is a `gethostname()` plus arithmetic,
        # and it cannot change without this process having been restarted.
        self._clock_domain = clock_domain()

    async def serve_forever(self) -> None:
        """Bind the socket and accept connections until cancelled.

        Unlinks any pre-existing file at `socket_path` first, because a prior executor crash can
        leave the socket file on disk, which would otherwise make `asyncio.start_unix_server`
        fail to bind with `OSError: [Errno 98] Address already in use`.

        Deliberately does not use `async with self._server:`, because `asyncio.Server`'s context
        manager `__aexit__` calls `close()` and awaits `wait_closed()` directly on the raw
        `Server`, bypassing this class's own `close()` (which closes active connections first).
        If cancellation happens while a connection is open, that would hit the same
        `wait_closed()` deadlock `close()`'s docstring describes. Callers are expected to call
        `close()` explicitly (e.g. in a `finally` block), as every caller in this codebase does.
        """
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # The blind unlink is safe because the entry point holds the kernel-released
        # single-instance flock before this runs: no *live* executor can own the file we
        # remove, only a crashed one's remains. Without that lock, two executors racing here
        # would each unlink the other's socket and both "win".
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self.socket_path)
        )
        # Owner-only, immediately after bind: anything that can connect to this socket can make
        # the executor place real orders. asyncio creates it with the process umask (typically
        # world-connectable 0755); narrowing to 0600 closes that window to other local users.
        os.chmod(self.socket_path, 0o600)
        await self._server.serve_forever()

    async def close(self) -> None:
        """Stop accepting new connections, close open ones, and wait for the listener to close.

        Does not cancel already-spawned fire tasks. A fire already accepted (and acked) should
        run to completion even if the server is shutting down.

        Open connections must be closed *before* `wait_closed()` is awaited: on Python 3.12+,
        `asyncio.Server.wait_closed()` waits for every connection handler task the server
        spawned to finish, and not only for the listening socket to close. `_handle_connection`'s
        read loop only returns once its peer disconnects, so with a still-open connection and
        nothing prompting the peer to disconnect, awaiting `wait_closed()` first deadlocks
        forever. Found via a hang in
        `tests/ipc/test_poller_client.py::test_reconnects_after_executor_restarts`. Closing
        tracked writers first, then the listener, fixes it.
        """
        for writer in list(self._active_writers):
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._active_writers.add(writer)
        try:
            while True:
                try:
                    body = await read_frame_body(reader)
                except asyncio.IncompleteReadError:
                    break  # peer closed the connection cleanly
                except ValueError:
                    logger.exception("oversized wake frame, closing connection")
                    break  # can't safely resync the stream after a bogus length prefix

                # Stamped *after* the read returns, not before it. `read_frame_body` blocks until
                # a frame arrives, so a stamp taken ahead of it measures the idle gap between
                # wakes instead of the cost of handling one. Under the burst-shaped benchmark
                # that made no visible difference (frames were always already buffered), but
                # against live traffic it made `wake_recv` mean 1181 ms against a <1 ms budget,
                # tracking the 1186 ms wake inter-arrival time almost exactly. `wake_recv` is
                # decode-plus-ack, per the poller/executor IPC design; this is where that span
                # begins.
                started_at_ms = _now_ms()
                started_at_ns = time.perf_counter_ns()
                received_at_ms = started_at_ms
                try:
                    message = decode_wake_message(body)
                except Exception as exc:
                    logger.exception("failed to decode wake message")
                    write_frame(
                        writer,
                        WakeAck(
                            schema_version=SCHEMA_VERSION,
                            correlation_id="",
                            received_at_ms=received_at_ms,
                            status="rejected",
                            reason=str(exc),
                        ),
                    )
                    await writer.drain()
                    continue

                write_frame(
                    writer,
                    WakeAck(
                        schema_version=SCHEMA_VERSION,
                        correlation_id=message.correlation_id,
                        received_at_ms=received_at_ms,
                        status="accepted",
                        reason=None,
                    ),
                )
                await writer.drain()

                # ended_at_ms/duration_ms: see the matching comment in
                # `poller_client.py::_write_loop`. ms wall-clock stays for started_at_ms/
                # ended_at_ms, but duration_ms comes from perf_counter_ns so it can actually
                # resolve the <1ms wake_recv budget.
                ended_at_ns = time.perf_counter_ns()
                self.telemetry_db.record_latency_event(
                    {
                        "correlation_id": message.correlation_id,
                        "stage": "wake_recv",
                        "started_at_ms": started_at_ms,
                        "ended_at_ms": _now_ms(),
                        "duration_ms": (ended_at_ns - started_at_ns) / 1_000_000,
                        "metadata_json": _DRY_RUN_METADATA[message.dry_run],
                    }
                )

                task = asyncio.create_task(self._handle_fire(message))
                self._fire_tasks.add(task)
                task.add_done_callback(self._fire_tasks.discard)
        finally:
            self._active_writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handle_fire(self, message: WakeMessage) -> None:
        # The order goes out at `wire_price_yes_dollars`, the YES-side price, per the YES-side
        # wire-price rule, and never at `kalshi_price`, which is the poller's edge-math cost
        # (`1 - yes_bid` for a
        # "no" decision) and is carried for logging only. A real fire without a tradeable wire
        # price is refused outright: `0.0` is what a pre-v3 frame defaults to, and it is also
        # what an empty book quotes, so there is nothing correct to send. Shadow fires pass
        # through, since they never reach the network, and refusing them would silently thin the
        # `detect_fire` sample (the detect-to-fire measurement design) to only well-quoted books.
        if not message.dry_run and not 0.0 < message.wire_price_yes_dollars < 1.0:
            logger.error(
                "%s: refusing real fire with untradeable wire price %r (correlation_id=%s, "
                "kalshi_price=%r): pre-v3 wake frame or empty book",
                message.market_ticker,
                message.wire_price_yes_dollars,
                message.correlation_id,
                message.kalshi_price,
            )
            return

        # Snap the wire price to the market's own price grid, read from its `price_ranges` at
        # ladder time, because Kalshi rejects off-grid prices outright. Every price this bot sends
        # originates from Kalshi's own quote strings, so today this is float-artifact armor
        # rather than a live correction; the point is that the grid is *read*, not assumed to
        # be cents forever (the docs warn new structures get introduced).
        wire_price = _snap_to_grid(message.wire_price_yes_dollars, message.price_ranges)

        if message.dry_run:
            # Shadow fires always run at the configured count so the timed path is identical
            # from wake to pre-send stop regardless of what the book looked like or what a
            # sizing model would have staked.
            count = self.fixed_order_contract_count
        else:
            # Fractional-Kelly stake off the wire-carried edge and price, floored at the
            # configured constant. See `execution.position_sizing`. `balance_cache` reflects
            # the executor's own periodic poll, never fetched here (`ENGINEERING.md` rule 1).
            count = size_position(
                edge=message.edge,
                kalshi_price=message.kalshi_price,
                balance_dollars=(
                    self.balance_cache.balance_dollars if self.balance_cache is not None else 0.0
                ),
                kelly_fraction=self.kelly_fraction,
                max_position_pct_of_balance=self.max_position_pct_of_balance,
                min_contract_count=self.fixed_order_contract_count,
            )
            # Then size to the resting liquidity the wake reported: a FOK for more contracts
            # than rest at the level is a guaranteed `fill_or_kill_insufficient_resting_volume`.
            # `0.0` means the quote carried no size, so the Kelly-sized count stands unmodified.
            if message.available_size_contracts > 0:
                count = min(count, int(message.available_size_contracts))
            if count < 1:
                # Size known and under one whole contract: nothing this order could legally
                # request would fill. The rejected row records the fixed floor (the table's
                # CHECK requires count > 0) with the real reason in error_message.
                self._record_rejected(
                    message,
                    self.fixed_order_contract_count,
                    wire_price,
                    f"insufficient resting volume: {message.available_size_contracts:g} "
                    "contracts at the level",
                )
                return

        # Kill switch, after the wire-price guard (a rejected row must satisfy the table's
        # price CHECK) and before anything else is spent on the fire. Real fires only: a
        # shadow fire places no order, and a halted bot deliberately keeps observing,
        # including its latency measurement. See `runtime/killswitch.py`.
        if not message.dry_run and self.kill_switch is not None and self.kill_switch.is_engaged():
            reason = self.kill_switch.reason() or "(no reason recorded)"
            logger.warning(
                "%s: kill switch engaged, refusing real fire (correlation_id=%s, reason=%s)",
                message.market_ticker,
                message.correlation_id,
                reason,
            )
            self._record_rejected(message, count, wire_price, f"kill switch engaged: {reason}")
            return

        # Risk gate, last before dispatch, the one chokepoint that still works when the
        # poller is wedged. `check()` is pure in-memory arithmetic (state pre-computed at
        # startup by `RiskGate.warm_start`), measured at ~15-20 us p99 at default caps, about
        # 0.1% of the 15 ms detect->fire budget. It scans the attempt and fill deques, which are
        # bounded by `max_attempts_per_day`, so that figure scales with that limit and only that.
        if not message.dry_run and self.risk_gate is not None:
            rejection = self.risk_gate.check(
                ticker=message.market_ticker,
                direction=message.direction,
                exchange_index=message.exchange_index,
                count=count,
                price_dollars=wire_price,
                now_ms=_now_ms(),
                correlation_group=message.correlation_group,
            )
            if rejection is not None:
                logger.warning(
                    "%s: risk gate rejected fire (correlation_id=%s): %s",
                    message.market_ticker,
                    message.correlation_id,
                    rejection,
                )
                self._record_rejected(message, count, wire_price, f"risk gate: {rejection}")
                return
            # Recorded pre-send so a hung or failed dispatch still counts against every cap.
            # The money may have moved regardless of what this process saw.
            self.risk_gate.record_attempt(
                message.market_ticker,
                message.direction,
                count,
                wire_price,
                _now_ms(),
                correlation_group=message.correlation_group,
            )

        gating_risk = self.risk_gate is not None and not message.dry_run
        if gating_risk and self.risk_gate is not None:
            self.risk_gate.begin_dispatch()

        timings = RequestTimings()
        # Held in a local and written afterwards, deliberately. `record_latency_event()` raises on
        # an unknown stage, and a raise here would be caught by the `except Exception` below and
        # logged as a dispatch failure while the order silently never went out. It can also take
        # a lock and emit a synchronous warning when the queue is full, and it makes the SQLite
        # writer thread runnable, which is a GIL context switch between building the order and
        # signing it.
        # Nothing that can raise, block, or log for a telemetry reason belongs between the
        # decision and the send; `ENGINEERING.md`'s rule 4 is the general form of this.
        order_build_ms: float | None = None
        try:
            build_start_ns = monotonic_ns()
            template = build_template(message.market_ticker, message.direction)
            order_build_ms = (monotonic_ns() - build_start_ns) / 1_000_000

            response = await self.dispatcher.dispatch(
                template,
                count=count,
                price_dollars=f"{wire_price:.4f}",
                time_in_force=self.order_time_in_force,
                self_trade_prevention_type=self.order_self_trade_prevention_type,
                exchange_index=message.exchange_index,
                correlation_id=message.correlation_id,
                correlation_group=message.correlation_group,
                timings=timings,
                dry_run=message.dry_run,
            )
            if gating_risk and self.risk_gate is not None:
                # Feed the response's fill back into the position cap, which is what makes it
                # a cap on positions instead of attempts. Defensive float parse: the field is
                # a fixed-point string on the wire.
                try:
                    fill_count = float(response.get("fill_count", 0) or 0)
                except (TypeError, ValueError):
                    fill_count = 0.0
                self.risk_gate.record_fill(message.market_ticker, fill_count, _now_ms())
        except Exception:
            # dispatch() already records orders_fired (success or failure) in its own finally
            # block and re-raises; this task has no caller to propagate to, so log and stop here
            # rather than let it surface only as an "exception was never retrieved" GC warning.
            logger.exception(
                "%s: dispatch failed for correlation_id=%s",
                message.market_ticker,
                message.correlation_id,
            )
        finally:
            if gating_risk and self.risk_gate is not None:
                self.risk_gate.end_dispatch()
            # In the `finally` so a failed or timed-out dispatch still yields whatever was
            # measured before it broke. A dispatch that timed out is the case where the timings
            # are most worth having, and it is exactly the case with no return value.
            self._record_request_timings(message, timings, order_build_ms)

    def _record_rejected(
        self, message: WakeMessage, count: int, price_dollars: float, reason: str
    ) -> None:
        """Write the `rejected` `orders_fired` row for a real fire this process refused.

        A blocked fire must be visible in the audit trail and never silently absent. An operator
        reading `orders_fired` after an incident needs to see what the bot declined to do and
        why, under the same correlation_id as its decision row. `RiskGate.warm_start` excludes
        these rows, so a refusal never counts as an attempt.

        The enqueue is pushed to the default thread pool rather than made on the event loop:
        `orders_fired` is an undroppable telemetry table, so on a full queue (a wedged SQLite
        writer, exactly the disk-pressure incident during which a rejection storm is most
        likely) `record_order_fired` degrades to a blocking 2 s `queue.put`. On the loop that
        stall would freeze a *concurrent* in-flight dispatch's response handling; on a pool
        thread it costs nothing the loop can feel. Flagged by @latency-profiler ahead of the
        first real fire. `TelemetryDB`'s queue is thread-safe, and rejected rows have no
        ordering dependency on anything else this task writes.
        """
        asyncio.get_running_loop().run_in_executor(
            None,
            self.telemetry_db.record_order_fired,
            {
                "correlation_id": message.correlation_id,
                "client_order_id": uuid.uuid4().hex,
                "kalshi_order_id": None,
                "ticker": message.market_ticker,
                "correlation_group": message.correlation_group or None,
                "outcome_side": message.direction,
                "count": f"{count}.00",
                "price_dollars": f"{price_dollars:.4f}",
                "time_in_force": self.order_time_in_force,
                "self_trade_prevention_type": self.order_self_trade_prevention_type,
                "status": "rejected",
                "error_message": reason,
                "requested_at_ms": _now_ms(),
                "submitted_at_ms": None,
                "acknowledged_at_ms": None,
            },
        )

    def _record_request_timings(
        self,
        message: WakeMessage,
        timings: RequestTimings,
        order_build_ms: float | None,
    ) -> None:
        """Write the per-stage rows `transport` measured, plus the end-to-end `detect_fire` row.

        Each stage is written only if it actually completed; a `None` span means that point was
        never reached (e.g. `dispatch_ack` on a request that timed out, or on any shadow fire),
        and a missing row is a more honest record of that than a zero would be.

        `order_build` is passed in rather than written where it was measured, so that no telemetry
        call sits between building the order and sending it. See `_handle_fire`.

        `detect_fire` is computed as `timings.sent_ns - message.recv_ns`, measured directly end
        to end and never summed from the stages. A sum would silently exclude every gap between
        them:
        queue waits, event-loop scheduling, and the IPC hop itself are most of the span on a
        healthy run, and they belong to no stage.
        """
        ended_at_ms = _now_ms()
        metadata_json = _DRY_RUN_METADATA[message.dry_run]
        for stage, duration_ms in (
            ("order_build", order_build_ms),
            ("sign", timings.sign_ms),
            ("dispatch_send", timings.dispatch_send_ms),
            ("dispatch_ack", timings.dispatch_ack_ms),
        ):
            if duration_ms is None:
                continue
            self.telemetry_db.record_latency_event(
                {
                    "correlation_id": message.correlation_id,
                    "stage": stage,
                    # Derived from `ended_at_ms` and the duration rather than both read as "now".
                    # These columns are meant to bracket the span, and the span has already
                    # happened by the time this runs, and two calls to the clock here would give
                    # two timestamps whose difference is zero while `duration_ms` says otherwise.
                    "started_at_ms": ended_at_ms - int(duration_ms),
                    "ended_at_ms": ended_at_ms,
                    "duration_ms": duration_ms,
                    "metadata_json": metadata_json,
                }
            )

        if timings.sent_ns == 0:
            return  # never got as far as handing the request over; there is no span to report
        if not message.is_clock_comparable_to(self._clock_domain):
            # Not an error worth raising, and not a row worth writing. Either the poller never
            # stamped the frame, or its clock is unrelated to this one. Logged at debug because
            # on a correctly-deployed single host it never fires, and if it does the operator
            # wants to know why the latency table is empty.
            logger.debug(
                "omitting detect_fire for %s: recv_ns=%d poller_domain=%r local_domain=%r",
                message.correlation_id,
                message.recv_ns,
                message.clock_domain,
                self._clock_domain,
            )
            return
        duration_ms = (timings.sent_ns - message.recv_ns) / 1_000_000
        self.telemetry_db.record_latency_event(
            {
                "correlation_id": message.correlation_id,
                "stage": "detect_fire",
                # Both endpoints in this process's wall clock, derived from the measured duration.
                # `message.decision_ts_ms` is tempting here and wrong: for a Kalshi quote it is the
                # *exchange's* `ts_ms` off the wire, so subtracting it from a local clock mixes two
                # unrelated time sources into a plausible-looking wrong number, the exact failure
                # `runtime/clock.py` exists to prevent, on the headline row of the whole phase.
                "started_at_ms": ended_at_ms - int(duration_ms),
                "ended_at_ms": ended_at_ms,
                "duration_ms": duration_ms,
                "metadata_json": metadata_json,
            }
        )
