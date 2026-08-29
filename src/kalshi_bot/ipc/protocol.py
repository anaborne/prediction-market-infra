"""Wire protocol for the poller->executor wake channel.

Framing is a 4-byte big-endian length prefix followed by an `orjson`-encoded body, chosen over
newline-delimited framing so the reader can `readexactly(4)` then `readexactly(length)` with no
delimiter-scanning, and so nothing relies on JSON output never containing a raw newline as a wire
invariant. See `docs/GUIDE.md`.

`encode_frame`/`decode_wake_message`/`decode_wake_ack` are pure functions over `bytes` with no
asyncio/socket dependency, so the encode/decode round trip is testable without a real connection.
`write_frame`/`read_frame_body` are thin `asyncio.StreamWriter`/`StreamReader` wrappers around
them, used by `executor_server.py` and `poller_client.py`.

`Direction`/`WakeAckStatus` are defined locally instead of imported from `decision.models`
(`OutcomeSide`), because the executor process must have zero dependency on `decision`, per this
package's design (see `docs/GUIDE.md`).
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field
from typing import Any, Literal

import orjson

SCHEMA_VERSION = 4

# Fields added after v1 (`recv_ns`/`clock_domain`/`dry_run` in v2; `wire_price_yes_dollars`,
# `exchange_index`, `available_size_contracts`, and `price_ranges` in v3; `correlation_group` in
# v4). Every one is defaulted on `WakeMessage`, so a v4 decoder reads a v1/v2/v3 frame (the
# absent fields take their defaults) and an older decoder reading a newer frame would fail on the
# unexpected keys, which is why the version is bumped instead of left alone. In practice both
# processes are deployed together from one ref; this is belt and braces for a rolling restart
# where the poller comes up before the executor does. The v3 default is load-bearing in a way the
# v2 ones are not: `wire_price_yes_dollars=0.0` is not a tradeable price, and the executor
# refuses to dispatch a real fire carrying it, so a v2 poller cannot make a v3 executor place a
# mispriced order. `correlation_group=""` (v4) is load-bearing the same way `clock_domain=""` is:
# it means "unknown", and the risk gate's group caps skip a fire carrying it instead of guessing
# a group, so a v3 poller cannot make a v4 executor apply the wrong group's cap.

_FRAME_LENGTH_BYTES = 4
# Far above any real wake message's size (a few hundred bytes), a sanity cap against a
# corrupted or malicious length prefix causing an unbounded `readexactly`.
_MAX_FRAME_BYTES = 64 * 1024

Direction = Literal["yes", "no"]
WakeAckStatus = Literal["accepted", "rejected"]


@dataclass(frozen=True, slots=True)
class WakeMessage:
    """Poller -> executor: a `should_fire=True` decision to act on.

    Deliberately excludes `count`/`time_in_force`/`self_trade_prevention_type`. Those are the
    executor's own fixed, config-driven values (`KalshiBotConfig`), and not the poller's
    business.

    Attributes:
        schema_version: Wire schema version, for future additive changes.
        correlation_id: The same id already written to this fire's `decision_results` row,
            passed through and never re-minted, so telemetry joins across processes.
        market_ticker: Kalshi market ticker to fire on.
        asset: Asset symbol (e.g. "BTC"), for executor-side logging only.
        direction: Which outcome side to take, "yes" or "no".
        kalshi_price: The effective cost the poller's edge math ran against, as a float in
            (0, 1). `yes_ask` for "yes", `1 - yes_bid` for "no". Executor-side logging only;
            not the price sent to Kalshi. Sending it for a "no" fire is the wrong-side bug
            recorded in `docs/GUIDE.md`.
        wire_price_yes_dollars: The YES-side price the executor puts on the wire, formatted to
            Kalshi's fixed-point dollar string at dispatch time. `yes_ask` for "yes" (buy YES),
            `yes_bid` for "no" (sell YES). Kalshi's order endpoint quotes everything from the YES
            side (the YES-side wire-price rule). Defaults to `0.0` (v3 field), which is not a
            tradeable price, and which the executor refuses to dispatch for real, so a pre-v3 frame
            degrades to a refused fire rather than a mispriced order.
        exchange_index: Demo shard the market lives on, from `StrikeWatch.exchange_index`, sent
            on the order so Kalshi routes it directly instead of paying the documented
            auto-routing latency. Defaults to `-1` (v3 field), Kalshi's own "require
            auto-routing by ticker" sentinel, so an unknown shard or a pre-v3 frame degrades to
            a correct-but-slower order, never a wrong one.
        available_size_contracts: Contracts resting at the level the order would take (ask side
            for "yes", bid side for "no"), from the triggering quote. The executor caps the FOK
            order count to this, since a FOK for more than rests is a guaranteed
            `fill_or_kill_insufficient_resting_volume`. `0.0` (the v3 default) means unknown:
            the executor falls back to its configured fixed count.
        price_ranges: The market's price grid, read from its own `price_ranges` at ladder time
            (off-grid prices are rejected by Kalshi). Each entry is `[start, end, step]`; a
            price is snapped to whichever range it falls in before formatting. Most markets
            have one range at a one-cent step; some (the 15-minute crypto series) have three,
            tighter in the tails. See `ingest.strike_ladder._price_ranges_of`. `list[list[float]]`
            instead of a tuple of tuples because `orjson` round-trips JSON arrays as lists, and
            this dataclass is decoded straight from `orjson.loads()`'s output via `cls(**data)`.
            Defaults to one flat one-cent range, matching every market that has only one.
        correlation_group: `AssetConfig.correlation_group` for this fire's asset, so the risk
            gate can cap exposure across tickers that settle against the same underlying (e.g.
            an hourly series and its 15-minute counterpart) instead of treating them as
            independent because they never share a ticker (`execution/risk.py`). Defaults to
            `""` (v4 field), meaning unknown, which the risk gate reads as "skip the group cap
            for this fire" instead of guessing a group.
        model_probability: The decision engine's estimated probability, for executor-side
            logging only, and not used by `dispatch()`.
        fee: Taker fee at `kalshi_price`, for executor-side logging only.
        edge: `model_probability` minus `kalshi_price` (sign per direction), for executor-side
            logging only.
        decision_ts_ms: When the decision engine evaluated this fire (`DecisionResult.ts_ms`).
        sent_at_ms: Poller wall-clock (`time.time_ns() // 1_000_000`) at the moment this message
            was enqueued for sending. Informational only, retained for cross-referencing against
            other wall-clock-stamped rows (e.g. `decision_ts_ms`). Millisecond resolution cannot
            itself confirm or refute the `<1ms` `wake_send` budget. See `sent_at_ns`.
        sent_at_ns: Poller monotonic clock (`time.perf_counter_ns()`) at the same moment as
            `sent_at_ms`, used instead of it to compute `wake_send`'s recorded `duration_ms` at
            nanosecond resolution. Added in Phase 9 per `docs/GUIDE.md`'s
            Consequences ("worth a Phase 9 decision: ... add higher-resolution timing"). Only
            meaningful within the poller process that set it (`perf_counter_ns` has no defined
            cross-process meaning), which is exactly how it's used. `poller_client.py` reads it
            back in the same process that wrote it.
        recv_ns: Poller monotonic clock at the instant the WebSocket frame that led to this
            decision finished parsing (`ingest`'s `recv_ns`, carried through `DecisionResult`).
            This is the origin of the `detect_fire` measurement: the executor subtracts it from
            its own `monotonic_ns()` at the moment the order goes out. Unlike `sent_at_ns`, this
            value is read in a *different* process than the one that set it, which is only valid
            while both share a monotonic clock, hence `clock_domain`. `0` means the input was
            never stamped, and the executor records no `detect_fire` row.
        clock_domain: The poller's `runtime.clock.clock_domain()`, host plus quantized boot
            time. The executor compares it against its own and omits the cross-process
            `detect_fire` row when they differ, instead of subtracting two unrelated counters
            and publishing a plausible-looking wrong number. Empty string on a v1 frame, which
            is likewise treated as "not comparable".
        dry_run: A shadow fire. The executor runs the full wake-handling path (decode, ack,
            template build, timing) and stops immediately before `dispatch()`, placing no order.
            This exists because the real thing fires a few times an hour at most, and a p99 over
            a hundred samples is not a p99. Shadow wakes are sent for a sampled fraction of
            *non-firing* decisions, so the measured path is the real one at real volume. See
            `docs/GUIDE.md`.
    """

    schema_version: int
    correlation_id: str
    market_ticker: str
    asset: str
    direction: Direction
    kalshi_price: float
    model_probability: float
    fee: float
    edge: float
    decision_ts_ms: int
    sent_at_ms: int
    sent_at_ns: int
    recv_ns: int = 0
    clock_domain: str = ""
    dry_run: bool = False
    wire_price_yes_dollars: float = 0.0
    exchange_index: int = -1
    available_size_contracts: float = 0.0
    price_ranges: list[list[float]] = field(default_factory=lambda: [[0.0, 1.0, 0.01]])
    correlation_group: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WakeMessage:
        return cls(**data)

    def is_clock_comparable_to(self, local_clock_domain: str) -> bool:
        """Whether this message's `recv_ns` may be subtracted from a local `monotonic_ns()`.

        Both conditions have to hold: the frame carried a stamp at all (v1 frames and unstamped
        inputs leave `recv_ns` at `0`), and it came from the same host and boot as the reader.
        A monotonic counter has no meaning across either boundary, and comparing across one yields
        no error, just a wrong number, which for a latency measurement is the worst outcome
        available. See `runtime/clock.py`.

        Args:
            local_clock_domain: The reading process's own `runtime.clock.clock_domain()`.

        Returns:
            `True` if a cross-process duration computed against `recv_ns` is meaningful.
        """
        return self.recv_ns > 0 and self.clock_domain == local_clock_domain != ""


@dataclass(frozen=True, slots=True)
class WakeAck:
    """Executor -> poller: receipt acknowledgment, best-effort, logging only.

    Means "frame parsed," and never "order placed". It is sent immediately after a `WakeMessage` is
    successfully read and decoded, before `dispatch()` is awaited, so Kalshi's network round
    trip never inflates the wake-latency measurement this ack helps compute. The poller never
    retries based on this ack; it exists purely for observability.

    Attributes:
        schema_version: Wire schema version, for future additive changes.
        correlation_id: Echoes the `WakeMessage.correlation_id` this acks.
        received_at_ms: Executor wall-clock when the frame finished parsing.
        status: `"accepted"` if the frame parsed and a fire task was spawned, `"rejected"` if
            the frame was malformed or otherwise couldn't be acted on.
        reason: Populated only when `status == "rejected"`.
    """

    schema_version: int
    correlation_id: str
    received_at_ms: int
    status: WakeAckStatus
    reason: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WakeAck:
        return cls(**data)


def encode_frame(payload: WakeMessage | WakeAck) -> bytes:
    """Encode a `WakeMessage`/`WakeAck` as a length-prefixed `orjson` frame.

    Passes `payload` to `orjson.dumps()` directly instead of via `dataclasses.asdict()`, because
    `orjson` serializes dataclasses natively, and `asdict()`'s recursive-copy machinery (built
    for arbitrarily nested dataclasses/containers) is unnecessary overhead for these flat,
    single-level dataclasses on a path this phase is specifically trying to keep fast.
    """
    body = orjson.dumps(payload)
    return struct.pack(">I", len(body)) + body


def decode_wake_message(data: bytes) -> WakeMessage:
    """Decode a `WakeMessage` frame body (post length-prefix)."""
    return WakeMessage.from_dict(orjson.loads(data))


def decode_wake_ack(data: bytes) -> WakeAck:
    """Decode a `WakeAck` frame body (post length-prefix)."""
    return WakeAck.from_dict(orjson.loads(data))


def write_frame(writer: asyncio.StreamWriter, payload: WakeMessage | WakeAck) -> None:
    """Write one encoded frame to `writer`. Does not `drain()`, so the caller decides when to."""
    writer.write(encode_frame(payload))


async def read_frame_body(reader: asyncio.StreamReader) -> bytes:
    """Read one frame's body (post length-prefix) from `reader`.

    Raises:
        asyncio.IncompleteReadError: The peer closed the connection mid-frame (or before
            sending one). Callers use this to detect disconnect.
        ValueError: The frame's declared length exceeds `_MAX_FRAME_BYTES`.
    """
    length_prefix = await reader.readexactly(_FRAME_LENGTH_BYTES)
    (length,) = struct.unpack(">I", length_prefix)
    if length > _MAX_FRAME_BYTES:
        raise ValueError(f"frame length {length} exceeds max {_MAX_FRAME_BYTES}")
    return await reader.readexactly(length)
