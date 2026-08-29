"""Order dispatch.

Turns a filled-in order into a signed request and sends it via the transport layer.

Targets Kalshi's current order-creation endpoint, `POST /portfolio/events/orders`, which is the
"V2" request/response shape (single-book `bid`/`ask` side, fixed-point dollar prices,
`time_in_force` instead of a `limit`/`market` type). This supersedes the legacy
`POST /portfolio/orders` shape
(`side` yes/no + `action` buy/sell + `type` limit/market + integer-cent price) that this module
and `telemetry/schema.sql` previously targeted. See
`docs/GUIDE.md` for why, and
`docs/GUIDE.md` (superseded) for the prior state.

Direction is expressed as `outcome_side` ("yes"/"no") on `PrebuiltOrderTemplate`, translated here
to the wire's `bid`/`ask` `side`. `BookSide` quotes everything from the YES side: `bid` means
buy YES, `ask` means *sell YES*. There is no NO-side price field, and a "no" position is sold
YES at the YES-side price (`yes_bid`), never at the `1 - price` complement. Callers own that
distinction: `price_dollars` must already be the YES-side wire price (see
`docs/GUIDE.md` for the bug that conflating the two
produced). There is no market-order type: Kalshi's current endpoint always requires an explicit
`price`; immediacy is instead expressed via `time_in_force` (e.g.
`immediate_or_cancel`/`fill_or_kill` against a marketable price for market-like execution,
`good_till_canceled` for a resting limit order).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any, Literal

from kalshi_bot.execution.prebuilt_orders import PrebuiltOrderTemplate
from kalshi_bot.telemetry.db import TelemetryDB
from kalshi_bot.transport.rest_client import ORDER_TIMEOUT, KalshiRestClient, RequestTimings

_ORDERS_PATH = "/portfolio/events/orders"

TimeInForce = Literal["fill_or_kill", "good_till_canceled", "immediate_or_cancel"]
SelfTradePreventionType = Literal["taker_at_cross", "maker"]
OrderStatus = Literal["pending", "submitted", "accepted", "filled", "canceled", "error"]

OrderGuard = Callable[[], None]
"""A no-argument callable that raises if placing an order right now is not permitted.

In production this is `config.KalshiBotConfig.assert_orders_permitted`, bound, the single
implementation of the "production orders need `KALSHI_ALLOW_PRODUCTION_ORDERS`" rule, kept in
`config` so there is exactly one home for it. Passing the bound method instead of the config
object keeps `execution` free of any dependency on the config *loader*, the same way `ipc` takes
only the two order-behaviour types from it.
"""


def permit_orders() -> None:
    """An `OrderGuard` that permits everything. For tests and fakes only.

    Named so that a real order path granting itself blanket permission is greppable, and so that
    no call site can do it by accident, since the constructor has no default.
    """
    return None


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _fixed_point_str(value: Any) -> str | None:
    """Pass a response's fixed-point string through verbatim; tolerate a bare number.

    `CreateOrderV2Response`'s `FixedPointCount`/`FixedPointDollars` fields serialize as strings
    (`"1.00"`, `"0.5600"`), the same convention `orders_fired` stores for `count`/`price_dollars`.
    Stored verbatim rather than parsed-and-reformatted so the audit trail records exactly what
    Kalshi said.
    """
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _fixed_point_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_post_only(time_in_force: TimeInForce, post_only: bool | None) -> bool:
    """Decide the `post_only` flag for an order, defaulting a resting order to maker-only.

    `post_only` is a real field on `CreateOrderV2Request` (verified against Kalshi's OpenAPI
    specification); its documented semantics are that a resting order which would take liquidity is
    cancelled rather than crossed. That is the single field the maker thesis depends on: without it,
    a quote posted at the touch that crosses on arrival silently becomes a taker and pays the
    quadratic taker fee, on exactly the trades where the market has just moved.

    The default is therefore on for `good_till_canceled` and off for `fill_or_kill` /
    `immediate_or_cancel`: those two time-in-force values exist to take liquidity immediately,
    and asking the exchange to reject any order that takes liquidity while also demanding
    immediate execution is a contradiction, not a safety measure. An explicit `post_only`
    argument overrides the default in either direction.

    Args:
        time_in_force: The order's time-in-force.
        post_only: Explicit caller choice, or `None` to take the default for `time_in_force`.

    Returns:
        The boolean to send as the request's `post_only`.
    """
    if post_only is not None:
        return post_only
    return time_in_force == "good_till_canceled"


def _resolved_status(response: dict[str, Any], time_in_force: TimeInForce) -> OrderStatus:
    """Resolve an order's recorded status from the fill data its `200` response carries.

    For a `fill_or_kill` order the response is the complete final state, since it either fully
    filled or died, so `orders_fired.status` can say which instead of the older, weaker
    `submitted`:

    - `fill_count > 0` → `filled` (for IOC a partial fill still records `filled`;
      `remaining_count` preserves the exact split).
    - `fill_count == 0` under FOK/IOC → `canceled`: the matching engine killed it, nothing rests.
    - `fill_count == 0` under GTC → `accepted`: the order is resting on the book.
    - No parseable `fill_count` at all → `submitted`: the request was accepted but this response
      shape carries nothing further to say, and inventing a stronger claim would corrupt the
      audit trail.
    """
    fill_count = _fixed_point_float(response.get("fill_count"))
    if fill_count is None:
        return "submitted"
    if fill_count > 0:
        return "filled"
    return "accepted" if time_in_force == "good_till_canceled" else "canceled"


def is_post_only_rejection(error: Exception) -> bool:
    """Whether this error is Kalshi refusing a `post_only` order that would have crossed.

    Measured, 2026-08-24: a `post_only` order priced through the touch comes back as HTTP 400
    with the generic code `invalid_order`, and not as a 200-with-cancel or a code that names the
    cause. So a maker loop sees an exception where it expected a response, and cannot tell "my
    quote would have taken liquidity, reprice" from "I built a malformed order" by code alone.

    This predicate is the narrowest honest reading of that: 400 plus `invalid_order`. It is
    not proof, because a genuinely malformed order returns the same pair, which is why the caller
    still owns the disambiguation, and owns it cheaply: it knows the price it sent and the touch
    it sent it against, so `post_only=True` plus a marketable price plus this shape is
    conclusive. What this function removes is the need for every such caller to re-derive the
    status code and the string.

    Args:
        error: The exception raised by `dispatch()`.

    Returns:
        Whether the error has the shape of a `post_only` cross rejection.
    """
    status = getattr(error, "status", None)
    return status == 400 and "invalid_order" in str(error)


class OrderDispatcher:
    """Dispatches orders to Kalshi via a `KalshiRestClient`.

    Attributes:
        rest_client: Client used to send order requests.
        telemetry_db: Sink for the fire-and-forget `orders_fired` record of each dispatch
            attempt. Per `docs/GUIDE.md`, `dispatch()` is the caller responsible for this write;
            `TelemetryDB.record_order_fired` only enqueues and never blocks, so holding it here does
            not put SQLite I/O on the hot path.
        assert_orders_permitted: The `OrderGuard` consulted before every dispatch.
    """

    def __init__(
        self,
        rest_client: KalshiRestClient,
        telemetry_db: TelemetryDB,
        assert_orders_permitted: OrderGuard,
    ) -> None:
        """Store the REST client, telemetry sink, and order-permission guard used by `dispatch()`.

        Args:
            rest_client: Client used to send order requests.
            telemetry_db: Sink for the fire-and-forget `orders_fired` record of each dispatch.
            assert_orders_permitted: Raises if orders are not permitted right now, in production
                `config.KalshiBotConfig.assert_orders_permitted`, bound. Required, with no
                default, deliberately: the production-order rule was previously enforced only at
                *process startup*, by whichever script remembered to call it, so any new caller
                that constructed a dispatcher and dispatched was outside it. Making it a
                constructor argument moves the guarantee from "every entry point remembers" to
                "the type cannot be built without one", the same mechanism-not-convention rule
                `docs/GUIDE.md` §7.3 applies to the other structural guarantees. Tests pass
                `permit_orders`.
        """
        self.rest_client = rest_client
        self.telemetry_db = telemetry_db
        self.assert_orders_permitted = assert_orders_permitted

    async def dispatch(
        self,
        template: PrebuiltOrderTemplate,
        count: int,
        price_dollars: str,
        time_in_force: TimeInForce,
        self_trade_prevention_type: SelfTradePreventionType,
        *,
        post_only: bool | None = None,
        exchange_index: int = -1,
        client_order_id: str | None = None,
        correlation_id: str | None = None,
        correlation_group: str = "",
        timings: RequestTimings | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Fill in a pre-built order template and send it.

        Generates `client_order_id` (the bot-side idempotency key `orders_fired` requires to be
        `UNIQUE`) and `correlation_id` (the join key across `orders_fired`/`market_snapshots`/
        `latency_events`) when not supplied. Callers retrying a logical order after an ambiguous
        failure (e.g. a timeout where the first attempt may have landed) should pass the same
        `client_order_id` back in instead of letting a new one be minted, so Kalshi's own
        idempotency check, and this bot's, can catch the duplicate.

        Fires a non-blocking `orders_fired` telemetry write after the dispatch call resolves
        (success or failure); the write is only enqueued (`TelemetryDB.record_order_fired`, per
        `docs/GUIDE.md`), never awaited, so it cannot delay or block the dispatch itself. The row
        captures the response's fill data (`fill_count`/`remaining_count`/`average_fill_price`/
        `average_fee_paid`, verbatim fixed-point strings) and resolves `status` from it
        (`_resolved_status`). For a fill_or_kill order that is complete fill reconciliation from
        the response already in hand, no extra request.

        Args:
            template: Pre-built order shape to fill in.
            count: Number of contracts to order. Formatted to Kalshi's fixed-point count string
                (e.g. `5` -> `"5.00"`) when building the request.
            price_dollars: Order price as Kalshi's fixed-point dollar string (e.g. `"0.5600"`),
                always required, since Kalshi's current endpoint has no separate market-order
                type.
                Must be the YES-side price regardless of `outcome_side` (`yes_bid` when
                selling YES for a "no" position, never `1 - yes_bid`). See the module docstring
                and the YES-side wire-price rule.
            time_in_force: `"fill_or_kill"`, `"good_till_canceled"`, or `"immediate_or_cancel"`.
            self_trade_prevention_type: `"taker_at_cross"` or `"maker"`, how Kalshi should
                handle this order matching against the bot's own resting order, if any.
            post_only: Whether Kalshi should cancel this order rather than let it cross and take
                liquidity. `None` (the default) means on for `good_till_canceled`, off for
                `fill_or_kill`/`immediate_or_cancel`. See `resolve_post_only`. Sent on every
                request, never omitted, so the audit trail records the flag that was in force
                rather than an exchange-side default this bot did not choose.
            exchange_index: Demo shard the market lives on, sent explicitly so Kalshi routes
                the order directly. The docs warn automatic routing "will incur an additional
                latency cost", which matters inside a 15 ms budget. `-1` (the default) is
                Kalshi's documented "require auto-routing by ticker" sentinel: correct, just
                slower, for callers that do not know the shard.
            client_order_id: Bot-generated idempotency key. Generated via `uuid4` if omitted.
            correlation_id: Join key for this fire's telemetry rows. Generated via `uuid4` if
                omitted.
            correlation_group: `AssetConfig.correlation_group` for this fire's asset, recorded
                on the `orders_fired` row so `execution.risk.RiskGate.warm_start()` can rebuild
                the per-group caps after a restart without `execution` importing `decision` to
                re-derive it. `""` (the default) means unknown.
            timings: Filled in by `transport` with this dispatch's monotonic stamps, including
                when the request fails. The caller writes the resulting `latency_events` rows,
                since `transport` cannot: it must not import `telemetry`. See
                `transport.rest_client.RequestTimings`.
            dry_run: A shadow fire. Everything up to and including signing and body assembly
                runs; the request is not sent, no `orders_fired` row is written, and `{}` is
                returned. No money moves and nothing enters the audit trail, because nothing was
                ordered, and the call exists only to time the path. See `docs/GUIDE.md`.

        Returns:
            Parsed order response from the Kalshi API, or `{}` for a `dry_run`.

        Raises:
            RuntimeError: If `assert_orders_permitted` refuses, which by default means when
                `KALSHI_ENVIRONMENT=prod` and `KALSHI_ALLOW_PRODUCTION_ORDERS` is not set true.
                Checked first, before the `client_order_id` is minted and before anything is signed
                or written, so a refused dispatch consumes no idempotency key and leaves no
                `orders_fired` row: it did not happen. A `dry_run` is checked too, since a shadow
                fire must traverse the identical path or the latency it measures is a different
                path's.
        """
        self.assert_orders_permitted()

        client_order_id = client_order_id or uuid.uuid4().hex
        correlation_id = correlation_id or uuid.uuid4().hex
        count_str = f"{count}.00"
        resolved_post_only = resolve_post_only(time_in_force, post_only)

        body: dict[str, Any] = {
            "ticker": template.ticker,
            "client_order_id": client_order_id,
            "side": "bid" if template.outcome_side == "yes" else "ask",
            "count": count_str,
            "price": price_dollars,
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
            "post_only": resolved_post_only,
            "exchange_index": exchange_index,
        }

        requested_at_ms = _now_ms()
        response: dict[str, Any] = {}
        status: OrderStatus = "error"
        error_message: str | None = None

        # Every column the row will ever have, filled in once and reused for both writes below.
        # Built here, before the request, so the `finally` block does no work it could avoid.
        row: dict[str, Any] = {
            "correlation_id": correlation_id,
            "client_order_id": client_order_id,
            "kalshi_order_id": None,
            "ticker": template.ticker,
            "correlation_group": correlation_group or None,
            "outcome_side": template.outcome_side,
            "count": count_str,
            "price_dollars": price_dollars,
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
            "post_only": int(resolved_post_only),
            "status": "pending",
            "error_message": None,
            "fill_count": None,
            "remaining_count": None,
            "average_fill_price": None,
            "average_fee_paid": None,
            "requested_at_ms": requested_at_ms,
            "submitted_at_ms": None,
            "acknowledged_at_ms": None,
        }

        # The pending row goes out before the request does. Without it, the only write is the
        # one in `finally`, so an order whose resolution never arrives (the process dies, the
        # event loop wedges, the write is lost) leaves *nothing at all*, and a real filled
        # position becomes invisible from inside the bot. That happened: see
        # `docs/GUIDE.md` section 6, a 41-contract fill with no
        # row of any kind. A `pending` row makes "we lost the write" distinguishable from "we lost
        # the order", and a `pending` row older than any plausible round trip is a defect anyone
        # can query for.
        #
        # `blocking=False` is what keeps this legal under `ENGINEERING.md` rule 4. `orders_fired` is
        # an
        # undroppable table, and an undroppable enqueue onto a saturated queue waits up to two
        # seconds, two seconds sitting directly in front of the socket write, which is exactly
        # what rule 4 forbids. Non-blocking, this is a `put_nowait` on a `deque`: tens of
        # nanoseconds, no I/O, no lock held across a syscall. A row lost that way is logged with
        # its `client_order_id`, counted in `dropped_count()`, and then recreated by the
        # resolution upsert, so dropping it costs a warning rather than the audit trail.
        if not dry_run:
            self.telemetry_db.record_order_fired(row, blocking=False)

        submitted_at_ms = _now_ms()
        try:
            # A fill-or-kill order that has not resolved in two seconds has already missed the
            # edge it was placed for. Without an explicit deadline this inherits aiohttp's
            # five-minute default, during which the executor is blind to what happened.
            response = await self.rest_client.post(
                _ORDERS_PATH, body, timeout=ORDER_TIMEOUT, timings=timings, dry_run=dry_run
            )
            status = _resolved_status(response, time_in_force)
        except Exception as exc:
            error_message = str(exc)
            raise
        finally:
            # A shadow fire placed no order, so it gets no `orders_fired` row. That table is the
            # record of what this bot did with money and the basis for the risk gate's warm
            # start; padding it with orders that never existed would corrupt both.
            #
            # The fill columns come straight off the response: for a fill_or_kill order this is
            # complete reconciliation, what the bot owns and what it paid, captured at no
            # extra request, on the response the dispatch already had in hand. Discarding it was
            # the sole reason "the bot never learns what it actually owns" was ever true.
            if not dry_run:
                # A new dict, never a mutation of `row`. `row` was handed to the telemetry
                # queue by reference and may still be sitting there unwritten; mutating it would
                # silently turn the queued `pending` row into the resolved one before the writer
                # ever saw it, so the in-flight state would never reach the database and this
                # whole mechanism would be a no-op in exactly the case it exists for. Building a
                # fresh dict also keeps the copy off the pre-dispatch path, since it happens here,
                # after the order is already on the wire.
                resolution = {
                    **row,
                    "kalshi_order_id": response.get("order_id"),
                    "status": status,
                    "error_message": error_message,
                    "fill_count": _fixed_point_str(response.get("fill_count")),
                    "remaining_count": _fixed_point_str(response.get("remaining_count")),
                    "average_fill_price": _fixed_point_str(response.get("average_fill_price")),
                    "average_fee_paid": _fixed_point_str(response.get("average_fee_paid")),
                    "submitted_at_ms": submitted_at_ms,
                    "acknowledged_at_ms": _now_ms() if status != "error" else None,
                }
                # An upsert on `client_order_id`, so it either upgrades the pending row above or
                # recreates it if that one was dropped. Blocking is fine here and everywhere in
                # this block: the order is already on the wire, so this is no longer the stretch
                # rule 4 protects.
                self.telemetry_db.record_order_resolution(resolution)
        return response

    async def cancel(
        self, order_id: str, *, exchange_index: int = -1, timeout: Any = ORDER_TIMEOUT
    ) -> dict[str, Any]:
        """Withdraw a resting order. `DELETE /portfolio/events/orders/{order_id}`.

        The dispatcher could place an order but not take one back, which made cancellation
        something every caller improvised against the raw transport, outside
        `assert_orders_permitted`, and outside anything that could record it. A maker cancels far
        more often than it places (task 2 measured that a reprice must be cancel-then-place,
        because amending a price forfeits queue priority), so this is the more frequent operation
        of the two, not an afterthought.

        Verified against demo, 2026-08-24: returns `{order_id, reduced_by, ts_ms}`, where
        `reduced_by` is the remaining count at the moment of cancellation, with `'2.72'` observed
        for a partially-filled 5-contract order. Cancelling an order that is already gone returns
        `404`.

        Args:
            order_id: Kalshi's order id, from the placement response's `order_id`.
            exchange_index: Shard to route to. `-1` asks Kalshi to auto-route, which is correct
                but slower.
            timeout: Per-request deadline. Defaults to the order timeout, since a cancel that has
                not landed in two seconds has left inventory exposed for two seconds.

        Returns:
            The parsed `CancelOrderV2Response`.

        Raises:
            RuntimeError: If `assert_orders_permitted` refuses. A cancel is checked by the same
                guard as a placement: it is an authenticated write against a real account, and a
                production-unarmed process has no business issuing one.
        """
        self.assert_orders_permitted()
        return await self.rest_client.delete(
            f"{_ORDERS_PATH}/{order_id}",
            params={"exchange_index": str(exchange_index)},
            timeout=timeout,
        )

    async def decrease(
        self,
        order_id: str,
        *,
        reduce_to: int | None = None,
        reduce_by: int | None = None,
        exchange_index: int = -1,
        timeout: Any = ORDER_TIMEOUT,
    ) -> dict[str, Any]:
        """Shrink a resting order without losing its place in the queue.

        This is the considered answer to "should `amend` be wired in", and the answer is: not
        `amend`, this. Task 2 measured the queue mechanics directly (`docs/GUIDE.md` section 2.11b)
        and Kalshi's OpenAPI specification states them on the amend path item:

        - Amending the price forfeits queue priority, so the order goes to the back. A
          reprice gains nothing from `amend` over cancel-then-place, and *loses* something:
          `AmendOrderV2Request` has no `post_only` field while `AmendOrderV2Response` carries
          `fill_count`/`average_fee_paid`, so an amend into a crossing price takes liquidity and
          pays the quadratic taker fee with none of the protection a fresh order gets.
        - Amending to a larger size also forfeits priority.
        - Reducing size is the one adjustment that keeps it.

        So the only amend-family operation worth having is the size reduction, and
        `POST /portfolio/events/orders/{order_id}/decrease` is the endpoint that does exactly
        that and nothing else: no price field, so it cannot cross, so the missing `post_only` on
        the amend shape cannot bite. Narrower endpoint, same benefit, none of the foot-gun.

        Exactly one of `reduce_to` / `reduce_by` must be given; the spec rejects both and neither.

        Args:
            order_id: Kalshi's order id.
            reduce_to: Target remaining count.
            reduce_by: Amount to subtract from the remaining count.
            exchange_index: Shard to route to; `-1` auto-routes.
            timeout: Per-request deadline.

        Returns:
            The parsed `DecreaseOrderV2Response`, `{order_id, remaining_count, ts_ms}`.

        Raises:
            ValueError: If not exactly one of `reduce_to`/`reduce_by` is given. Raised here rather
                than letting the exchange reject it, because the failure is a bug in the caller
                and a round trip is a poor way to learn that.
            RuntimeError: If `assert_orders_permitted` refuses.
        """
        if (reduce_to is None) == (reduce_by is None):
            raise ValueError(
                "decrease() needs exactly one of reduce_to or reduce_by; "
                f"got reduce_to={reduce_to!r}, reduce_by={reduce_by!r}"
            )
        self.assert_orders_permitted()
        body: dict[str, Any] = {"exchange_index": exchange_index}
        if reduce_to is not None:
            body["reduce_to"] = f"{reduce_to}.00"
        else:
            body["reduce_by"] = f"{reduce_by}.00"
        return await self.rest_client.post(
            f"{_ORDERS_PATH}/{order_id}/decrease", body, timeout=timeout
        )
