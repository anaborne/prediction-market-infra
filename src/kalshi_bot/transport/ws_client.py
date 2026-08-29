"""WebSocket client for the Kalshi API.

Wraps a streaming connection to a Kalshi WebSocket endpoint for consuming market data updates.

`base_url` (from `KalshiBotConfig.demo_ws_url`/`prod_ws_url`) is the WS host only, e.g.
`wss://external-api-ws.demo.kalshi.co`, and it does not include the `/trade-api/ws/v2` path. This
client owns that path (`_WS_PATH` below), which must match
`kalshi_bot.auth.signer`'s fixed WebSocket auth path exactly, since the auth handshake signs that
literal path.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any, Final

import orjson
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosedOK

from kalshi_bot.auth.signer import KalshiRequestSigner

_WS_PATH = "/trade-api/ws/v2"

# `websockets`' own ping/pong keepalive (20s interval, 20s timeout by default) sends and expects
# traffic independently of application data, so a connection that is genuinely alive produces
# *something* well inside this window even with no subscribed market trading. 90s gives that
# keepalive room to do its job before this client's own watchdog fires, while still catching the
# failure mode that keepalive alone did not (see `messages()`'s docstring).
_DEFAULT_MESSAGE_TIMEOUT_SECONDS: Final[float] = 90.0


class KalshiWebSocketClient:
    """Async WebSocket client for streaming Kalshi market data.

    Attributes:
        base_url: Base WebSocket URL of the target Kalshi environment (demo or prod), host only.
        api_key_id: Kalshi API key identifier sent in the `KALSHI-ACCESS-KEY` header.
        signer: Request signer used to authenticate the connection handshake.
        message_timeout_seconds: Longest gap allowed between received frames before `messages()`
            raises `TimeoutError`. See `messages()`'s docstring for why this exists.
    """

    def __init__(
        self,
        base_url: str,
        api_key_id: str,
        signer: KalshiRequestSigner,
        message_timeout_seconds: float = _DEFAULT_MESSAGE_TIMEOUT_SECONDS,
    ) -> None:
        """Store the base URL, key id, and signer for later use by connection methods.

        Args:
            base_url: Base WebSocket URL of the target Kalshi environment, host only (no
                `/trade-api/ws/v2`).
            api_key_id: Kalshi API key identifier for the `KALSHI-ACCESS-KEY` header.
            signer: Request signer used to authenticate the connection handshake.
            message_timeout_seconds: Longest gap allowed between received frames before
                `messages()` raises `TimeoutError`. Defaults to `_DEFAULT_MESSAGE_TIMEOUT_SECONDS`.
        """
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self.signer = signer
        self.message_timeout_seconds = message_timeout_seconds
        self._connection: ClientConnection | None = None
        self._next_command_id = 1

    async def connect(self) -> None:
        """Establish the WebSocket connection."""
        timestamp = str(int(time.time() * 1000))
        signature = self.signer.sign_websocket_auth(timestamp)
        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }
        self._connection = await websockets.connect(
            f"{self.base_url}{_WS_PATH}", additional_headers=headers
        )

    async def subscribe(
        self,
        channels: list[str],
        market_tickers: list[str] | None = None,
        send_initial_snapshot: bool = False,
        use_yes_price: bool | None = None,
    ) -> None:
        """Subscribe to the given market data channels.

        Args:
            channels: Channel names to subscribe to.
            market_tickers: Market tickers to scope the subscription to, e.g. for the `ticker`/
                `orderbook_delta` channels, per `docs.kalshi.com/asyncapi.yaml` (verified directly,
                see `docs/GUIDE.md`), these channels accept a `market_tickers` array covering many
                tickers in one subscription. Omitted (`None`) for channels that take no market
                filter, e.g. `market_lifecycle_v2`.
            send_initial_snapshot: Ask Kalshi to emit the current state of each subscribed market
                immediately on subscribe, rather than only on the next change. Verified live
                against demo (`scripts/live_ws_smoke_test.py`): the `ticker` channel sends
                nothing at all until a field changes, so an idle market subscribed without this
                yields no quote for as long as it stays idle. Sent as a parameter rather than
                assumed, since the field is only honored by channels that document it.
            use_yes_price: Put both sides of `orderbook_delta`/`orderbook_snapshot` on the
                yes price scale. By default Kalshi reports no-side levels in *no-leg* pricing, so
                the two sides use different scales and toggling sides flips the price. Verified
                by A/B on 2026-08-24 (`docs/GUIDE.md` §2.10): the identical 113,183.01-contract
                level came back at `0.0100` without the flag and `0.9900` with it. Kalshi intends
                to flip the default later, so a consumer that cares must say which it wants
                instead of inheriting a default that is scheduled to change. This repository has
                got the `100 − x` conversion wrong three times (§6). `None` sends nothing and
                takes whatever the server's current default is.
        """
        connection = self._require_connection()
        params: dict[str, Any] = {"channels": channels}
        if market_tickers is not None:
            params["market_tickers"] = market_tickers
        if send_initial_snapshot:
            params["send_initial_snapshot"] = True
        if use_yes_price is not None:
            params["use_yes_price"] = use_yes_price
        command = {
            "id": self._next_command_id,
            "cmd": "subscribe",
            "params": params,
        }
        self._next_command_id += 1
        await connection.send(orjson.dumps(command).decode("utf-8"))

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed messages received on the connection.

        Each receive is bounded by `message_timeout_seconds`. The `ticker` channel is
        change-driven, so an idle *market* can go quiet indefinitely (see `subscribe()`), but the
        *connection* should never go this long without any frame at all, since `websockets`' own
        ping/pong keepalive sends and expects traffic independently of application data. A
        connection that stops producing anything for this long is wedged, not idle: raising here
        hands it to the caller's reconnect wrapper (`ingest.reconnect.reconnecting_stream`)
        instead of silently starving the pipeline. Closes the gap behind a real incident
        (2026-08-21) where a stalled leg produced no data for ~25 minutes without ever raising or
        logging a reconnect attempt.

        Yields:
            Parsed JSON messages from the WebSocket stream.

        Raises:
            TimeoutError: No message (data or otherwise) arrived within `message_timeout_seconds`.
        """
        connection = self._require_connection()
        while True:
            try:
                async with asyncio.timeout(self.message_timeout_seconds) as deadline:
                    raw_message = await connection.recv()
            except TimeoutError as exc:
                # `TimeoutError` is also `socket.timeout`/what any future internal timeout
                # inside `recv()` itself could raise. `deadline.expired()` confirms this is
                # actually *our* watchdog before asserting that specific narrative in the
                # message, rather than mislabeling an unrelated timeout as connection silence.
                if not deadline.expired():
                    raise
                raise TimeoutError(
                    f"no message received on Kalshi WS within {self.message_timeout_seconds}s"
                ) from exc
            except ConnectionClosedOK:
                # Matches `async for message in connection`'s behavior: a clean remote close
                # ends this generator normally rather than propagating as an error.
                return
            parsed: dict[str, Any] = orjson.loads(raw_message)
            yield parsed

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._connection is not None:
            await self._connection.close()
        self._connection = None

    def _require_connection(self) -> ClientConnection:
        if self._connection is None:
            raise RuntimeError("KalshiWebSocketClient.connect() must be called before use")
        return self._connection
