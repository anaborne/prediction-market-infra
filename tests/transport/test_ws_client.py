"""Tests for `KalshiWebSocketClient`.

Runs a local `websockets` server bound to an ephemeral `127.0.0.1` port and points the client at
it, and never at the live Kalshi demo API. No test in this suite touches a live API; the manual,
opt-in live check lives in `scripts/live_smoke_test.py`.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import orjson
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from websockets.asyncio.server import Server, ServerConnection
from websockets.asyncio.server import serve as ws_serve
from websockets.http11 import Request

from kalshi_bot.auth.signer import KalshiRequestSigner
from kalshi_bot.transport.ws_client import KalshiWebSocketClient

_API_KEY_ID = "test-key-id"


def _write_rsa_key(path: Path) -> rsa.RSAPublicKey:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return private_key.public_key()


def _verify_signature(public_key: rsa.RSAPublicKey, message: str, signature_b64: str) -> None:
    public_key.verify(
        base64.b64decode(signature_b64),
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


@pytest.fixture
def public_key(tmp_path: Path) -> rsa.RSAPublicKey:
    return _write_rsa_key(tmp_path / "test_key.pem")


@pytest.fixture
def signer(tmp_path: Path, public_key: rsa.RSAPublicKey) -> KalshiRequestSigner:
    return KalshiRequestSigner(tmp_path / "test_key.pem")


def _base_url(server: Server) -> str:
    host, port = server.sockets[0].getsockname()[:2]
    return f"ws://{host}:{port}"


async def test_connect_sends_the_key_header_and_signs_the_fixed_ws_auth_message(
    signer: KalshiRequestSigner, public_key: rsa.RSAPublicKey
) -> None:
    captured_headers: dict[str, str] = {}

    def process_request(connection: ServerConnection, request: Request) -> None:
        captured_headers.update(request.headers)
        return None

    async def handler(connection: ServerConnection) -> None:
        async for _ in connection:
            pass

    async with ws_serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        client = KalshiWebSocketClient(_base_url(server), _API_KEY_ID, signer)
        await client.connect()
        try:
            # `websockets`'s `Headers` normalizes names to lowercase when captured into a dict;
            # header names are case-insensitive per RFC 7230, so this doesn't affect wire behavior.
            assert captured_headers["kalshi-access-key"] == _API_KEY_ID
            _verify_signature(
                public_key,
                f"{captured_headers['kalshi-access-timestamp']}GET/trade-api/ws/v2",
                captured_headers["kalshi-access-signature"],
            )
        finally:
            await client.close()


async def test_subscribe_sends_a_json_command_with_an_incrementing_id(
    signer: KalshiRequestSigner,
) -> None:
    received_commands: list[dict[str, Any]] = []

    async def handler(connection: ServerConnection) -> None:
        async for raw_message in connection:
            command = orjson.loads(raw_message)
            received_commands.append(command)
            ack = orjson.dumps({"type": "subscribed", "id": command["id"]})
            await connection.send(ack.decode())

    async with ws_serve(handler, "127.0.0.1", 0) as server:
        client = KalshiWebSocketClient(_base_url(server), _API_KEY_ID, signer)
        await client.connect()
        try:
            message_iterator = client.messages()

            await client.subscribe(["ticker"])
            first_ack = await anext(message_iterator)
            await client.subscribe(["orderbook_delta"])
            second_ack = await anext(message_iterator)
        finally:
            await client.close()

    assert first_ack == {"type": "subscribed", "id": 1}
    assert second_ack == {"type": "subscribed", "id": 2}
    assert received_commands == [
        {"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker"]}},
        {"id": 2, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"]}},
    ]


async def test_messages_yields_parsed_json_messages_from_the_server(
    signer: KalshiRequestSigner,
) -> None:
    async def handler(connection: ServerConnection) -> None:
        await connection.send(orjson.dumps({"type": "ticker", "seq": 1}).decode())
        await connection.send(orjson.dumps({"type": "ticker", "seq": 2}).decode())

    async with ws_serve(handler, "127.0.0.1", 0) as server:
        client = KalshiWebSocketClient(_base_url(server), _API_KEY_ID, signer)
        await client.connect()
        try:
            received = [message async for message in client.messages()]
        finally:
            await client.close()

    assert received == [{"type": "ticker", "seq": 1}, {"type": "ticker", "seq": 2}]


async def test_messages_times_out_when_the_connection_goes_silent(
    signer: KalshiRequestSigner,
) -> None:
    """A connection that stops producing *any* frame, ticker updates included, must raise instead
    of hanging forever, the failure mode a real incident hit (2026-08-21): the process stayed
    heartbeating while the pipeline behind it produced no data for ~25 minutes, with nothing ever
    raising or logging a reconnect attempt.
    """
    server_should_go_silent = asyncio.Event()

    async def handler(connection: ServerConnection) -> None:
        del connection
        await server_should_go_silent.wait()

    async with ws_serve(handler, "127.0.0.1", 0) as server:
        client = KalshiWebSocketClient(
            _base_url(server), _API_KEY_ID, signer, message_timeout_seconds=0.05
        )
        await client.connect()
        try:
            with pytest.raises(TimeoutError):
                await anext(client.messages())
        finally:
            server_should_go_silent.set()
            await client.close()


async def test_messages_ends_cleanly_on_a_normal_remote_close(
    signer: KalshiRequestSigner,
) -> None:
    async def handler(connection: ServerConnection) -> None:
        await connection.send(orjson.dumps({"type": "ticker", "seq": 1}).decode())

    async with ws_serve(handler, "127.0.0.1", 0) as server:
        client = KalshiWebSocketClient(_base_url(server), _API_KEY_ID, signer)
        await client.connect()
        try:
            received = [message async for message in client.messages()]
        finally:
            await client.close()

    assert received == [{"type": "ticker", "seq": 1}]


async def test_subscribe_before_connect_raises_runtime_error(
    signer: KalshiRequestSigner,
) -> None:
    client = KalshiWebSocketClient("ws://127.0.0.1:0", _API_KEY_ID, signer)

    with pytest.raises(RuntimeError):
        await client.subscribe(["ticker"])


async def test_messages_before_connect_raises_runtime_error(
    signer: KalshiRequestSigner,
) -> None:
    client = KalshiWebSocketClient("ws://127.0.0.1:0", _API_KEY_ID, signer)

    with pytest.raises(RuntimeError):
        await anext(client.messages())


async def test_close_before_connect_is_a_no_op(signer: KalshiRequestSigner) -> None:
    client = KalshiWebSocketClient("ws://127.0.0.1:0", _API_KEY_ID, signer)

    await client.close()
