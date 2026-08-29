"""Tests for `ipc.protocol`.

Encode/decode round trips are pure-function tests against raw `bytes`. `write_frame`/
`read_frame_body` are exercised against an in-process `asyncio.StreamReader` fed directly via
`feed_data`/`feed_eof`, so no real socket is needed at this layer.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import orjson
import pytest

from kalshi_bot.ipc.protocol import (
    SCHEMA_VERSION,
    WakeAck,
    WakeMessage,
    decode_wake_ack,
    decode_wake_message,
    encode_frame,
    read_frame_body,
)

_WAKE_MESSAGE = WakeMessage(
    schema_version=SCHEMA_VERSION,
    correlation_id="corr-1",
    market_ticker="KXBTCD-T100",
    asset="BTC",
    direction="yes",
    kalshi_price=0.56,
    model_probability=0.62,
    fee=0.02,
    edge=0.06,
    decision_ts_ms=1_000,
    sent_at_ms=1_001,
    sent_at_ns=1_001_000_000,
    wire_price_yes_dollars=0.56,
    exchange_index=2,
)

_WAKE_ACK = WakeAck(
    schema_version=SCHEMA_VERSION,
    correlation_id="corr-1",
    received_at_ms=1_002,
    status="accepted",
    reason=None,
)


def test_wake_message_round_trips_through_encode_and_decode() -> None:
    frame = encode_frame(_WAKE_MESSAGE)
    body = frame[4:]

    decoded = decode_wake_message(body)

    assert decoded == _WAKE_MESSAGE


def test_wake_ack_round_trips_through_encode_and_decode() -> None:
    frame = encode_frame(_WAKE_ACK)
    body = frame[4:]

    decoded = decode_wake_ack(body)

    assert decoded == _WAKE_ACK


def test_wake_ack_rejected_status_carries_a_reason() -> None:
    ack = WakeAck(
        schema_version=SCHEMA_VERSION,
        correlation_id="corr-2",
        received_at_ms=2_000,
        status="rejected",
        reason="malformed frame",
    )

    decoded = decode_wake_ack(encode_frame(ack)[4:])

    assert decoded.status == "rejected"
    assert decoded.reason == "malformed frame"


def test_encode_frame_length_prefix_matches_body_length() -> None:
    frame = encode_frame(_WAKE_MESSAGE)
    length_prefix = int.from_bytes(frame[:4], "big")

    assert length_prefix == len(frame) - 4


async def test_read_frame_body_reads_exactly_one_frame() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(encode_frame(_WAKE_MESSAGE))
    reader.feed_eof()

    body = await read_frame_body(reader)

    assert decode_wake_message(body) == _WAKE_MESSAGE


async def test_read_frame_body_reads_two_sequential_frames() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(encode_frame(_WAKE_MESSAGE) + encode_frame(_WAKE_ACK))
    reader.feed_eof()

    first = decode_wake_message(await read_frame_body(reader))
    second = decode_wake_ack(await read_frame_body(reader))

    assert first == _WAKE_MESSAGE
    assert second == _WAKE_ACK


async def test_read_frame_body_raises_incomplete_read_on_early_close() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x00\x00\x00\x10")  # declares a 16-byte body, then closes
    reader.feed_eof()

    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame_body(reader)


async def test_read_frame_body_rejects_a_frame_declaring_too_large_a_length() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data((10 * 1024 * 1024).to_bytes(4, "big"))
    reader.feed_eof()

    with pytest.raises(ValueError, match="exceeds max"):
        await read_frame_body(reader)


def test_v2_decoder_reads_a_v1_frame() -> None:
    """A frame written before `recv_ns`/`clock_domain`/`dry_run` existed still decodes.

    Every added field is defaulted, so a v1 body, which simply has no such keys, round-trips
    into a `WakeMessage` whose new fields hold their defaults. This is what makes a rolling
    restart survivable in the window where the poller and executor disagree on version.
    """
    v1_body = orjson.dumps(
        {
            "schema_version": 1,
            "correlation_id": "corr-v1",
            "market_ticker": "KXBTCD-T100",
            "asset": "BTC",
            "direction": "yes",
            "kalshi_price": 0.56,
            "model_probability": 0.62,
            "fee": 0.02,
            "edge": 0.06,
            "decision_ts_ms": 1_000,
            "sent_at_ms": 1_001,
            "sent_at_ns": 1_001_000_000,
        }
    )

    decoded = decode_wake_message(v1_body)

    assert decoded.correlation_id == "corr-v1"
    assert decoded.recv_ns == 0
    assert decoded.clock_domain == ""
    assert decoded.dry_run is False


def test_v2_fields_round_trip() -> None:
    message = replace(
        _WAKE_MESSAGE, recv_ns=5_000_000_000, clock_domain="host-a:1787290000", dry_run=True
    )

    decoded = decode_wake_message(encode_frame(message)[4:])

    assert decoded == message


def test_v3_decoder_reads_a_v2_frame_with_an_untradeable_wire_price_default() -> None:
    """A frame written before `wire_price_yes_dollars` existed decodes to a refusable value.

    The default `0.0` is deliberate: it is not a tradeable price, and the executor refuses to
    dispatch a real fire carrying it, so in a rolling-restart window a v2 poller cannot make a v3
    executor place an order at a price the poller never computed (see the YES-side wire-price rule
    for what the pre-v3 price field actually held for a "no" fire).
    """
    v2_body = orjson.dumps(
        {
            "schema_version": 2,
            "correlation_id": "corr-v2",
            "market_ticker": "KXBTCD-T100",
            "asset": "BTC",
            "direction": "no",
            "kalshi_price": 0.30,
            "model_probability": 0.10,
            "fee": 0.02,
            "edge": 0.15,
            "decision_ts_ms": 1_000,
            "sent_at_ms": 1_001,
            "sent_at_ns": 1_001_000_000,
            "recv_ns": 5_000_000_000,
            "clock_domain": "host-a:1787290000",
            "dry_run": False,
        }
    )

    decoded = decode_wake_message(v2_body)

    assert decoded.correlation_id == "corr-v2"
    assert decoded.wire_price_yes_dollars == 0.0
    # -1 is Kalshi's own "require auto-routing by ticker" sentinel, so a pre-v3 frame routes
    # correctly, just without the explicit-shard latency win.
    assert decoded.exchange_index == -1


def test_v3_wire_price_round_trips() -> None:
    message = replace(_WAKE_MESSAGE, direction="no", kalshi_price=0.30, wire_price_yes_dollars=0.70)

    decoded = decode_wake_message(encode_frame(message)[4:])

    assert decoded == message
    assert decoded.wire_price_yes_dollars == 0.70


def test_v4_decoder_reads_a_v3_frame_with_an_unknown_correlation_group_default() -> None:
    """A frame written before `correlation_group` existed decodes to an unknown group.

    The default `""` is deliberate: the risk gate reads it as "skip the group cap for this
    fire" rather than guessing a group, so a rolling-restart window cannot make a v4 executor
    apply the wrong group's cap to a v3 poller's frame.
    """
    v3_body = orjson.dumps(
        {
            "schema_version": 3,
            "correlation_id": "corr-v3",
            "market_ticker": "KXBTCD-T100",
            "asset": "BTC",
            "direction": "yes",
            "kalshi_price": 0.56,
            "model_probability": 0.62,
            "fee": 0.02,
            "edge": 0.06,
            "decision_ts_ms": 1_000,
            "sent_at_ms": 1_001,
            "sent_at_ns": 1_001_000_000,
            "wire_price_yes_dollars": 0.56,
            "exchange_index": 2,
            "price_ranges": [[0.0, 1.0, 0.01]],
        }
    )

    decoded = decode_wake_message(v3_body)

    assert decoded.correlation_id == "corr-v3"
    assert decoded.correlation_group == ""


def test_v4_correlation_group_round_trips() -> None:
    message = replace(_WAKE_MESSAGE, correlation_group="majors")

    decoded = decode_wake_message(encode_frame(message)[4:])

    assert decoded == message
    assert decoded.correlation_group == "majors"


def test_clock_comparable_only_within_one_domain() -> None:
    message = replace(_WAKE_MESSAGE, recv_ns=5_000_000_000, clock_domain="host-a:1787290000")

    assert message.is_clock_comparable_to("host-a:1787290000")
    # Different host, or the same host after a reboot: the counters are unrelated and their
    # difference is a number with no meaning, which is worse than no number at all.
    assert not message.is_clock_comparable_to("host-b:1787290000")
    assert not message.is_clock_comparable_to("host-a:1787290060")


def test_unstamped_message_is_never_clock_comparable() -> None:
    """`recv_ns == 0` means nothing stamped it, so there is no origin to measure from."""
    message = replace(_WAKE_MESSAGE, recv_ns=0, clock_domain="host-a:1787290000")

    assert not message.is_clock_comparable_to("host-a:1787290000")


def test_empty_domains_do_not_match_each_other() -> None:
    """Two processes that both failed to identify themselves are not thereby comparable."""
    message = replace(_WAKE_MESSAGE, recv_ns=5_000_000_000, clock_domain="")

    assert not message.is_clock_comparable_to("")
