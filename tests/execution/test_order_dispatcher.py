"""Tests for `OrderDispatcher`.

Uses a fake `KalshiRestClient` (no live network) and a real
`TelemetryDB` against a temp SQLite file, so the fire-and-forget `orders_fired` write is verified
end-to-end the same way `tests/telemetry/test_db.py` verifies `TelemetryDB` itself: read back
after `close()` flushes the background writer thread.

`dispatch()` targets Kalshi's current `POST /portfolio/events/orders` shape. See
`docs/GUIDE.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from kalshi_bot.config import load_config
from kalshi_bot.execution.order_dispatcher import (
    OrderDispatcher,
    TimeInForce,
    is_post_only_rejection,
    permit_orders,
    resolve_post_only,
)
from kalshi_bot.execution.prebuilt_orders import build_template
from kalshi_bot.telemetry.db import TelemetryDB
from kalshi_bot.transport.rest_client import ORDER_TIMEOUT, RequestTimings


class _FakeRestClient:
    """Records every `post()` call instead of sending it anywhere."""

    def __init__(
        self, response: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.timeouts: list[aiohttp.ClientTimeout | None] = []
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
        self.timeouts.append(timeout)
        if self._error is not None:
            raise self._error
        return self._response


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def telemetry_db(tmp_path: Path) -> TelemetryDB:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    return db


async def test_dispatch_yes_side_sends_bid(telemetry_db: TelemetryDB) -> None:
    rest_client = _FakeRestClient(response={"order_id": "ko-1"})
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]
    template = build_template("KXTEST", "yes")

    result = await dispatcher.dispatch(
        template,
        count=10,
        price_dollars="0.4200",
        time_in_force="good_till_canceled",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    assert result == {"order_id": "ko-1"}
    assert len(rest_client.posted) == 1
    path, body = rest_client.posted[0]
    assert path == "/portfolio/events/orders"
    assert body["ticker"] == "KXTEST"
    assert body["side"] == "bid"
    assert body["count"] == "10.00"
    assert body["price"] == "0.4200"
    assert body["time_in_force"] == "good_till_canceled"
    assert body["self_trade_prevention_type"] == "taker_at_cross"
    # Callers that don't know the shard get Kalshi's documented "require auto-routing by
    # ticker" sentinel, correct and slower than an explicit index.
    assert body["exchange_index"] == -1


async def test_dispatch_sends_an_explicit_exchange_index(telemetry_db: TelemetryDB) -> None:
    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]
    template = build_template("KXTEST", "yes")

    await dispatcher.dispatch(
        template,
        count=1,
        price_dollars="0.4200",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
        exchange_index=2,
    )
    telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["exchange_index"] == 2


async def test_dispatch_no_side_sends_ask(telemetry_db: TelemetryDB) -> None:
    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]
    template = build_template("KXTEST", "no")

    await dispatcher.dispatch(
        template,
        count=1,
        price_dollars="0.7700",
        time_in_force="immediate_or_cancel",
        self_trade_prevention_type="maker",
    )
    telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["side"] == "ask"


async def test_dispatch_generates_client_order_id_and_correlation_id(
    telemetry_db: TelemetryDB,
) -> None:
    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]
    template = build_template("KXTEST", "yes")

    await dispatcher.dispatch(
        template,
        count=1,
        price_dollars="0.5000",
        time_in_force="good_till_canceled",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["client_order_id"]

    conn = _connect(telemetry_db.db_path)
    try:
        order_row = conn.execute("SELECT * FROM orders_fired").fetchone()
        correlation_row = conn.execute("SELECT * FROM correlations").fetchone()
    finally:
        conn.close()
    assert order_row["client_order_id"] == body["client_order_id"]
    assert order_row["correlation_id"] == correlation_row["correlation_id"]


async def test_dispatch_reuses_supplied_ids(telemetry_db: TelemetryDB) -> None:
    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]
    template = build_template("KXTEST", "yes")

    await dispatcher.dispatch(
        template,
        count=1,
        price_dollars="0.5000",
        time_in_force="good_till_canceled",
        self_trade_prevention_type="taker_at_cross",
        client_order_id="my-client-id",
        correlation_id="my-correlation-id",
    )
    telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["client_order_id"] == "my-client-id"

    conn = _connect(telemetry_db.db_path)
    try:
        order_row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()
    assert order_row["client_order_id"] == "my-client-id"
    assert order_row["correlation_id"] == "my-correlation-id"


async def test_dispatch_records_submitted_order_fired_row(telemetry_db: TelemetryDB) -> None:
    rest_client = _FakeRestClient(response={"order_id": "ko-9"})
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]
    template = build_template("KXTEST", "yes")

    await dispatcher.dispatch(
        template,
        count=5,
        price_dollars="0.3300",
        time_in_force="good_till_canceled",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()

    assert row["status"] == "submitted"
    assert row["kalshi_order_id"] == "ko-9"
    assert row["outcome_side"] == "yes"
    assert row["count"] == "5.00"
    assert row["price_dollars"] == "0.3300"
    assert row["time_in_force"] == "good_till_canceled"
    assert row["self_trade_prevention_type"] == "taker_at_cross"
    assert row["error_message"] is None
    assert row["acknowledged_at_ms"] is not None


async def test_dispatch_records_the_correlation_group(telemetry_db: TelemetryDB) -> None:
    """The risk gate's warm start rebuilds its per-group caps from this column."""
    rest_client = _FakeRestClient(response={"order_id": "ko-10"})
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]
    template = build_template("KXBTC15M-T100", "yes")

    await dispatcher.dispatch(
        template,
        count=1,
        price_dollars="0.5000",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
        correlation_group="majors",
    )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()

    assert row["correlation_group"] == "majors"


async def test_dispatch_defaults_correlation_group_to_null(telemetry_db: TelemetryDB) -> None:
    rest_client = _FakeRestClient(response={"order_id": "ko-11"})
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]
    template = build_template("KXTEST", "yes")

    await dispatcher.dispatch(
        template,
        count=1,
        price_dollars="0.5000",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()

    assert row["correlation_group"] is None


async def test_dispatch_records_error_status_and_reraises(telemetry_db: TelemetryDB) -> None:
    rest_client = _FakeRestClient(error=RuntimeError("boom"))
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]
    template = build_template("KXTEST", "yes")

    with pytest.raises(RuntimeError, match="boom"):
        await dispatcher.dispatch(
            template,
            count=1,
            price_dollars="0.5000",
            time_in_force="good_till_canceled",
            self_trade_prevention_type="taker_at_cross",
        )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()

    assert row["status"] == "error"
    assert row["error_message"] == "boom"
    assert row["kalshi_order_id"] is None
    assert row["acknowledged_at_ms"] is None


async def test_dispatch_uses_the_tight_order_timeout(telemetry_db: TelemetryDB) -> None:
    # Without an explicit deadline the order path inherits aiohttp's 5-minute default, during
    # which the executor has no idea whether the order landed.
    rest_client = _FakeRestClient(response={"order_id": "ko-1"})
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=1,
        price_dollars="0.4200",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    assert rest_client.timeouts == [ORDER_TIMEOUT]


async def test_dispatch_captures_fill_data_and_records_filled(telemetry_db: TelemetryDB) -> None:
    """A FOK that fills records the response's complete reconciliation, verbatim.

    `CreateOrderV2Response` carries `fill_count`/`remaining_count`/`average_fill_price`/
    `average_fee_paid` as fixed-point strings; discarding them was the sole reason "the bot never
    learns what it actually owns" was ever true. Shape mirrors the spec's response schema, so the
    values are strings, not numbers, matching how `FixedPointCount`/`FixedPointDollars`
    serialize.
    """
    rest_client = _FakeRestClient(
        response={
            "order_id": "ko-fill",
            "fill_count": "3.00",
            "remaining_count": "0.00",
            "average_fill_price": "0.5600",
            "average_fee_paid": "0.0200",
            "ts_ms": 1_787_300_000_000,
        }
    )
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=3,
        price_dollars="0.5600",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()

    assert row["status"] == "filled"
    assert row["fill_count"] == "3.00"
    assert row["remaining_count"] == "0.00"
    assert row["average_fill_price"] == "0.5600"
    assert row["average_fee_paid"] == "0.0200"


async def test_dispatch_records_canceled_for_a_killed_fok(telemetry_db: TelemetryDB) -> None:
    """A FOK with zero fills was killed by the matching engine: nothing rests, nothing owned."""
    rest_client = _FakeRestClient(
        response={"order_id": "ko-killed", "fill_count": "0.00", "remaining_count": "0.00"}
    )
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=1,
        price_dollars="0.4200",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()

    assert row["status"] == "canceled"
    assert row["average_fill_price"] is None  # spec: only present when fill_count > 0


async def test_dispatch_records_accepted_for_a_resting_gtc(telemetry_db: TelemetryDB) -> None:
    """A GTC with zero immediate fills is resting on the book, not dead."""
    rest_client = _FakeRestClient(
        response={"order_id": "ko-rest", "fill_count": "0.00", "remaining_count": "1.00"}
    )
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=1,
        price_dollars="0.4200",
        time_in_force="good_till_canceled",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()

    assert row["status"] == "accepted"
    assert row["remaining_count"] == "1.00"


async def test_dispatch_error_row_has_null_fill_columns(telemetry_db: TelemetryDB) -> None:
    """A failed dispatch has no response to read, so the fill columns must stay NULL, never zero."""
    rest_client = _FakeRestClient(error=RuntimeError("boom"))
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="boom"):
        await dispatcher.dispatch(
            build_template("KXTEST", "yes"),
            count=1,
            price_dollars="0.5000",
            time_in_force="fill_or_kill",
            self_trade_prevention_type="taker_at_cross",
        )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT * FROM orders_fired").fetchone()
    finally:
        conn.close()

    assert row["status"] == "error"
    assert row["fill_count"] is None
    assert row["remaining_count"] is None


#
# `post_only` is the one `CreateOrderV2Request` field the maker thesis depends on: a resting order
# that would take liquidity is cancelled rather than crossed, so a quote posted at the touch can
# never silently become a taker and pay the quadratic fee. Verified present in Kalshi's OpenAPI
# specification (`CreateOrderV2Request.post_only`). These tests
# pin what this bot *sends*; what the exchange *does* with it is measured in demo by
# `scripts/live_post_only_probe.py`, which no test may run (`ENGINEERING.md` rule 5).


@pytest.mark.parametrize(
    ("time_in_force", "expected"),
    [
        ("good_till_canceled", True),
        ("fill_or_kill", False),
        ("immediate_or_cancel", False),
    ],
)
async def test_dispatch_defaults_post_only_from_time_in_force(
    telemetry_db: TelemetryDB, time_in_force: TimeInForce, expected: bool
) -> None:
    """A resting order defaults to maker-only; the two take-now time-in-force values do not."""
    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=1,
        price_dollars="0.4200",
        time_in_force=time_in_force,
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["post_only"] is expected


@pytest.mark.parametrize("explicit", [True, False])
async def test_dispatch_explicit_post_only_overrides_the_default(
    telemetry_db: TelemetryDB, explicit: bool
) -> None:
    """An explicit argument wins in both directions, including against the GTC default."""
    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=1,
        price_dollars="0.4200",
        time_in_force="good_till_canceled",
        self_trade_prevention_type="taker_at_cross",
        post_only=explicit,
    )
    telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body["post_only"] is explicit


async def test_dispatch_always_sends_post_only(telemetry_db: TelemetryDB) -> None:
    """The field is never omitted. An absent flag would leave the exchange default in charge."""
    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.dispatch(
        build_template("KXTEST", "no"),
        count=1,
        price_dollars="0.4200",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="maker",
    )
    telemetry_db.close()

    _, body = rest_client.posted[0]
    assert "post_only" in body


async def test_dispatch_records_post_only_on_the_orders_fired_row(
    telemetry_db: TelemetryDB,
) -> None:
    """Whether a fill paid the maker or the taker fee is not recoverable from the response."""
    rest_client = _FakeRestClient(response={"order_id": "ko-1", "fill_count": "0.00"})
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=1,
        price_dollars="0.4200",
        time_in_force="good_till_canceled",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        row = conn.execute("SELECT post_only FROM orders_fired").fetchone()
    finally:
        conn.close()

    assert row["post_only"] == 1


@pytest.mark.parametrize(
    ("time_in_force", "post_only", "expected"),
    [
        ("good_till_canceled", None, True),
        ("fill_or_kill", None, False),
        ("immediate_or_cancel", None, False),
        ("good_till_canceled", False, False),
        ("fill_or_kill", True, True),
    ],
)
def test_resolve_post_only(
    time_in_force: TimeInForce, post_only: bool | None, expected: bool
) -> None:
    assert resolve_post_only(time_in_force, post_only) is expected


#
# `config.assert_orders_permitted` was previously called only at *process startup*, by whichever
# entry point remembered to. Any new caller that built a dispatcher and dispatched was outside the
# guarantee entirely. The guard is now a required constructor argument consulted on every
# dispatch, so the guarantee is enforced by the type instead of by every author remembering,
# the same mechanism-not-convention rule `docs/GUIDE.md` §7.3 applies elsewhere.


async def test_dispatch_consults_the_order_guard_before_anything_else(
    telemetry_db: TelemetryDB,
) -> None:
    """A refused dispatch must not reach the network, mint an id, or leave an audit row.

    "It did not happen" is the honest record. A `client_order_id` burned or an `orders_fired` row
    written for an order the guard refused would corrupt both the idempotency key space and the
    record of what this bot did with money.
    """

    def refuse() -> None:
        raise RuntimeError("KALSHI_ALLOW_PRODUCTION_ORDERS is not true")

    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, refuse)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="KALSHI_ALLOW_PRODUCTION_ORDERS"):
        await dispatcher.dispatch(
            build_template("KXTEST", "yes"),
            count=1,
            price_dollars="0.5000",
            time_in_force="fill_or_kill",
            self_trade_prevention_type="taker_at_cross",
        )
    telemetry_db.close()

    assert rest_client.posted == []
    conn = _connect(telemetry_db.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders_fired").fetchone()[0] == 0
    finally:
        conn.close()


async def test_a_dry_run_is_guarded_too(telemetry_db: TelemetryDB) -> None:
    """A shadow fire must traverse the identical path, guard included, or it times a shorter one."""
    calls: list[int] = []
    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, lambda: calls.append(1))  # type: ignore[arg-type]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=1,
        price_dollars="0.5000",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
        dry_run=True,
    )
    telemetry_db.close()

    assert calls == [1]


async def test_a_real_config_can_be_wired_straight_in(telemetry_db: TelemetryDB) -> None:
    """The production wiring is `config.assert_orders_permitted` bound, so pin that it fits.

    `execution` deliberately does not import the config *loader*; it takes an `OrderGuard`
    callable. This test is the one place the two halves are checked against each other, so a
    signature change on either side fails here rather than at 3am in `scripts/run_executor.py`.
    """
    config = load_config(
        {
            "KALSHI_ENVIRONMENT": "prod",
            "KALSHI_DEMO_BASE_URL": "https://demo.example",
            "KALSHI_PROD_BASE_URL": "https://prod.example",
        }
    )
    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(
        rest_client,  # type: ignore[arg-type]
        telemetry_db,
        config.assert_orders_permitted,
    )

    with pytest.raises(RuntimeError, match="KALSHI_ALLOW_PRODUCTION_ORDERS"):
        await dispatcher.dispatch(
            build_template("KXTEST", "yes"),
            count=1,
            price_dollars="0.5000",
            time_in_force="fill_or_kill",
            self_trade_prevention_type="taker_at_cross",
        )
    telemetry_db.close()

    assert rest_client.posted == []


#
# Before this existed, the ONLY orders_fired write happened in `finally`, after the request
# resolved. An order whose resolution never arrived (process killed, loop wedged, write lost)
# left no row at all, and a real filled position became invisible from inside the bot. That is not
# hypothetical: a 41-contract fill on 2026-08-21 has no row of any kind
# (`docs/GUIDE.md` section 6). These tests pin the fix.


class _HangingRestClient:
    """A `post()` that never returns, the shape that used to leave no trace whatsoever."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def post(
        self,
        path: str,
        body: dict[str, Any],
        timeout: aiohttp.ClientTimeout | None = None,
        timings: RequestTimings | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self.entered.set()
        await asyncio.Event().wait()  # never resolves
        raise AssertionError("unreachable")


async def test_an_in_flight_dispatch_is_visible_as_a_pending_row(
    telemetry_db: TelemetryDB, tmp_path: Path
) -> None:
    """While the request is on the wire and nothing has come back, the row must already exist.

    This is the property the whole mechanism buys. Read from a *separate* connection while the
    dispatch is still in flight, so nothing about the flush is being taken on trust.
    """
    rest_client = _HangingRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    task = asyncio.create_task(
        dispatcher.dispatch(
            build_template("KXTEST", "yes"),
            count=7,
            price_dollars="0.6300",
            time_in_force="fill_or_kill",
            self_trade_prevention_type="taker_at_cross",
        )
    )
    try:
        await asyncio.wait_for(rest_client.entered.wait(), timeout=5)
        row = None
        for _ in range(100):  # the writer thread commits in batches; give it a moment
            conn = _connect(telemetry_db.db_path)
            try:
                row = conn.execute("SELECT * FROM orders_fired").fetchone()
            finally:
                conn.close()
            if row is not None:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        telemetry_db.close()

    assert row is not None, "an order on the wire must be visible before it resolves"
    assert row["status"] == "pending"
    assert row["count"] == "7.00"
    assert row["price_dollars"] == "0.6300"
    assert row["requested_at_ms"] > 0
    # Nothing may be claimed about an outcome nobody has observed yet.
    assert row["kalshi_order_id"] is None
    assert row["fill_count"] is None
    assert row["acknowledged_at_ms"] is None


async def test_a_dispatch_whose_resolution_never_lands_leaves_a_pending_row(
    telemetry_db: TelemetryDB,
) -> None:
    """The failure shape that used to be invisible: the order goes out, the write never lands.

    A process killed mid-flight, or a resolution write lost, leaves no `finally` behind. Before
    the pending row existed the result was *no row at all*, a real filled position with nothing
    in the audit trail to show for it. Now it is a queryable `pending`.
    """
    rest_client = _FakeRestClient(response={"order_id": "ko-1", "fill_count": "5.00"})
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]
    # The resolution never reaches the database, exactly as if the process died first.
    telemetry_db.record_order_resolution = lambda order: None  # type: ignore[method-assign]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=5,
        price_dollars="0.6300",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        rows = conn.execute("SELECT * FROM orders_fired").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, "silence is the one outcome this must never produce"
    assert rows[0]["status"] == "pending"
    # And it is datable, so "pending far longer than any round trip" is a query, not a guess.
    assert rows[0]["requested_at_ms"] > 0


async def test_the_pending_row_is_written_before_the_request_is_sent(
    telemetry_db: TelemetryDB,
) -> None:
    """Ordering is the whole mechanism: a row written after the send protects nothing."""
    sequence: list[str] = []
    real_record = telemetry_db.record_order_fired

    def spy(order: dict[str, Any], *, blocking: bool = True) -> None:
        sequence.append(f"telemetry(blocking={blocking})")
        real_record(order, blocking=blocking)

    class _RecordingClient(_FakeRestClient):
        async def post(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            sequence.append("post")
            return await super().post(*args, **kwargs)

    telemetry_db.record_order_fired = spy  # type: ignore[method-assign]
    dispatcher = OrderDispatcher(_RecordingClient(), telemetry_db, permit_orders)  # type: ignore[arg-type]
    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=1,
        price_dollars="0.5000",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    assert sequence[0] == "telemetry(blocking=False)", "the pending row must precede the send"
    assert sequence[1] == "post"


async def test_the_pending_write_never_blocks_the_hot_path(telemetry_db: TelemetryDB) -> None:
    """`ENGINEERING.md` rule 4: no telemetry write may delay an order reaching the wire.

    `orders_fired` is undroppable, and an undroppable enqueue onto a saturated queue waits up to
    two seconds. Two seconds in front of the socket write is precisely what rule 4 forbids, so the
    pre-dispatch write must pass `blocking=False`.
    """
    seen: list[bool] = []
    real_record = telemetry_db.record_order_fired

    def spy(order: dict[str, Any], *, blocking: bool = True) -> None:
        seen.append(blocking)
        real_record(order, blocking=blocking)

    telemetry_db.record_order_fired = spy  # type: ignore[method-assign]
    dispatcher = OrderDispatcher(_FakeRestClient(), telemetry_db, permit_orders)  # type: ignore[arg-type]
    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=1,
        price_dollars="0.5000",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    assert seen == [False]


async def test_the_resolution_upgrades_the_pending_row_in_place(
    telemetry_db: TelemetryDB,
) -> None:
    """One order, one row. The pending row is upgraded, never duplicated."""
    rest_client = _FakeRestClient(
        response={"order_id": "ko-1", "fill_count": "3.00", "remaining_count": "0.00"}
    )
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=3,
        price_dollars="0.5000",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        rows = conn.execute("SELECT * FROM orders_fired").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    (row,) = rows
    assert row["status"] == "filled"
    assert row["kalshi_order_id"] == "ko-1"
    assert row["fill_count"] == "3.00"
    assert row["acknowledged_at_ms"] is not None


async def test_a_resolution_recreates_a_pending_row_that_was_dropped(
    telemetry_db: TelemetryDB,
) -> None:
    """The pending write is best-effort; the resolution is not.

    Under a saturated queue the non-blocking pending write is dropped. An UPDATE would then match
    nothing and the order would vanish, the exact hole this mechanism closes. The upsert must
    recreate it.
    """
    dispatcher = OrderDispatcher(
        _FakeRestClient(response={"order_id": "ko-9", "fill_count": "0.00"}),  # type: ignore[arg-type]
        telemetry_db,
        permit_orders,
    )
    # Simulate the drop: the pending write goes nowhere at all.
    telemetry_db.record_order_fired = lambda order, *, blocking=True: None  # type: ignore[method-assign]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=2,
        price_dollars="0.5000",
        time_in_force="good_till_canceled",
        self_trade_prevention_type="taker_at_cross",
    )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        rows = conn.execute("SELECT status, kalshi_order_id FROM orders_fired").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, "a dropped pending row must be recreated, not silently lost"
    assert rows[0]["status"] == "accepted"
    assert rows[0]["kalshi_order_id"] == "ko-9"


async def test_a_dry_run_writes_no_pending_row(telemetry_db: TelemetryDB) -> None:
    """A shadow fire ordered nothing, so it must not appear as an in-flight order."""
    dispatcher = OrderDispatcher(_FakeRestClient(), telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.dispatch(
        build_template("KXTEST", "yes"),
        count=1,
        price_dollars="0.5000",
        time_in_force="fill_or_kill",
        self_trade_prevention_type="taker_at_cross",
        dry_run=True,
    )
    telemetry_db.close()

    conn = _connect(telemetry_db.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders_fired").fetchone()[0] == 0
    finally:
        conn.close()


#
# The dispatcher could place an order but not take one back, so every caller improvised a cancel
# against the raw transport, outside `assert_orders_permitted` and outside anything that could
# record it. A maker cancels more often than it places (task 2: a reprice must be
# cancel-then-place, because amending a price forfeits queue priority), so this is the more
# frequent half of the order path, not an afterthought.


class _DeletingRestClient(_FakeRestClient):
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        super().__init__(response=response)
        self.deleted: list[tuple[str, dict[str, Any] | None]] = []

    async def delete(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
        timings: RequestTimings | None = None,
    ) -> dict[str, Any]:
        self.deleted.append((path, params))
        return {"order_id": "ko-1", "reduced_by": "2.72", "ts_ms": 1}


async def test_cancel_targets_the_order_and_routes_to_its_shard(
    telemetry_db: TelemetryDB,
) -> None:
    rest_client = _DeletingRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    result = await dispatcher.cancel("ko-1", exchange_index=2)
    telemetry_db.close()

    assert result == {"order_id": "ko-1", "reduced_by": "2.72", "ts_ms": 1}
    assert rest_client.deleted == [("/portfolio/events/orders/ko-1", {"exchange_index": "2"})]


async def test_cancel_passes_the_production_order_guard(telemetry_db: TelemetryDB) -> None:
    """A cancel is an authenticated write against a real account, so the same gate applies."""

    def refuse() -> None:
        raise RuntimeError("KALSHI_ALLOW_PRODUCTION_ORDERS is not true")

    rest_client = _DeletingRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, refuse)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="KALSHI_ALLOW_PRODUCTION_ORDERS"):
        await dispatcher.cancel("ko-1")
    telemetry_db.close()

    assert rest_client.deleted == []


async def test_decrease_sends_reduce_to_and_keeps_the_order_id(
    telemetry_db: TelemetryDB,
) -> None:
    """The one amend-family operation that preserves queue priority, so the one worth having."""
    rest_client = _FakeRestClient(response={"order_id": "ko-1", "remaining_count": "1.00"})
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.decrease("ko-1", reduce_to=1, exchange_index=2)
    telemetry_db.close()

    path, body = rest_client.posted[0]
    assert path == "/portfolio/events/orders/ko-1/decrease"
    assert body == {"exchange_index": 2, "reduce_to": "1.00"}


async def test_decrease_sends_reduce_by_when_that_is_what_was_asked(
    telemetry_db: TelemetryDB,
) -> None:
    rest_client = _FakeRestClient(response={})
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    await dispatcher.decrease("ko-1", reduce_by=3)
    telemetry_db.close()

    _, body = rest_client.posted[0]
    assert body == {"exchange_index": -1, "reduce_by": "3.00"}


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"reduce_to": 1, "reduce_by": 1}],
    ids=["neither", "both"],
)
async def test_decrease_refuses_before_the_round_trip(
    telemetry_db: TelemetryDB, kwargs: dict[str, int]
) -> None:
    """The spec rejects both and neither; a round trip is a poor way to learn about a caller bug."""
    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(rest_client, telemetry_db, permit_orders)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="exactly one"):
        await dispatcher.decrease("ko-1", **kwargs)
    telemetry_db.close()

    assert rest_client.posted == []


class _HttpError(Exception):
    """The shape `is_post_only_rejection` reads: a `status` attribute and a message.

    A stand-in rather than a real `aiohttp.ClientResponseError`, which needs a live
    `RequestInfo` to render its own string. The predicate deliberately reads only `status` and
    `str(error)`, so this exercises exactly the surface it uses.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def test_a_post_only_cross_rejection_is_recognisable() -> None:
    """Measured shape: HTTP 400 with the generic code `invalid_order`.

    A maker loop must treat it as "reprice", not as a fault. Kalshi gives no distinguishing code,
    so this is the narrowest honest reading and the caller still owns the final call.
    """
    assert is_post_only_rejection(_HttpError(400, "400 invalid_order: invalid order")) is True


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (409, "409 fill_or_kill_insufficient_resting_volume: ..."),
        (404, "404 user_not_found: ..."),
        (400, "400 something_else: ..."),
    ],
)
def test_other_failures_are_not_mistaken_for_a_post_only_rejection(
    status: int, message: str
) -> None:
    assert is_post_only_rejection(_HttpError(status, message)) is False


def test_a_plain_exception_is_not_a_post_only_rejection() -> None:
    assert is_post_only_rejection(RuntimeError("boom")) is False
