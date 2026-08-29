"""Tests for `KalshiRestClient`.

Runs a local `aiohttp.web` server bound to an ephemeral `127.0.0.1` port and points the client at
it, and never at the live Kalshi demo API. No test in this suite touches a live API; the manual,
opt-in live check lives in `scripts/live_smoke_test.py`.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import aiohttp
import orjson
import pytest
from aiohttp import ClientResponseError, web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bot.auth.signer import KalshiRequestSigner
from kalshi_bot.transport import rest_client as rest_client_module
from kalshi_bot.transport.rest_client import KalshiRestClient, RequestTimings

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
    # public_key's fixture already wrote the key file at this path.
    return KalshiRequestSigner(tmp_path / "test_key.pem")


async def _client_for(
    app: web.Application, signer: KalshiRequestSigner
) -> tuple[KalshiRestClient, TestServer]:
    server = TestServer(app)
    await server.start_server()
    client = KalshiRestClient(str(server.make_url("")), _API_KEY_ID, signer)
    return client, server


async def test_get_signs_the_request_and_returns_the_parsed_json_body(
    signer: KalshiRequestSigner, public_key: rsa.RSAPublicKey
) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: web.Request) -> web.Response:
        captured["path"] = request.path
        captured["headers"] = dict(request.headers)
        return web.json_response({"ticker": "INXD-24"})

    app = web.Application()
    app.router.add_get("/trade-api/v2/markets/{ticker}", handler)
    client, server = await _client_for(app, signer)
    try:
        result = await client.get("/markets/INXD-24")
    finally:
        await client.close()
        await server.close()

    assert result == {"ticker": "INXD-24"}
    assert captured["path"] == "/trade-api/v2/markets/INXD-24"
    headers = captured["headers"]
    assert headers["KALSHI-ACCESS-KEY"] == _API_KEY_ID
    _verify_signature(
        public_key,
        f"{headers['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/markets/INXD-24",
        headers["KALSHI-ACCESS-SIGNATURE"],
    )


async def test_get_excludes_query_params_from_the_signed_path(
    signer: KalshiRequestSigner, public_key: rsa.RSAPublicKey
) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: web.Request) -> web.Response:
        captured["query_string"] = request.query_string
        captured["headers"] = dict(request.headers)
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/trade-api/v2/markets", handler)
    client, server = await _client_for(app, signer)
    try:
        await client.get("/markets", params={"limit": "5"})
    finally:
        await client.close()
        await server.close()

    assert captured["query_string"] == "limit=5"
    headers = captured["headers"]
    # The signed message must exclude the query string even though the server received it.
    _verify_signature(
        public_key,
        f"{headers['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/markets",
        headers["KALSHI-ACCESS-SIGNATURE"],
    )


async def test_post_sends_an_orjson_encoded_body_and_signs_the_request(
    signer: KalshiRequestSigner, public_key: rsa.RSAPublicKey
) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: web.Request) -> web.Response:
        captured["body"] = orjson.loads(await request.read())
        captured["content_type"] = request.headers.get("Content-Type")
        captured["headers"] = dict(request.headers)
        return web.json_response({"order_id": "abc123"}, status=201)

    app = web.Application()
    app.router.add_post("/trade-api/v2/portfolio/orders", handler)
    client, server = await _client_for(app, signer)
    try:
        result = await client.post(
            "/portfolio/orders", {"ticker": "INXD-24", "side": "yes", "count": 1}
        )
    finally:
        await client.close()
        await server.close()

    assert result == {"order_id": "abc123"}
    assert captured["body"] == {"ticker": "INXD-24", "side": "yes", "count": 1}
    assert captured["content_type"] == "application/json"
    headers = captured["headers"]
    _verify_signature(
        public_key,
        f"{headers['KALSHI-ACCESS-TIMESTAMP']}POST/trade-api/v2/portfolio/orders",
        headers["KALSHI-ACCESS-SIGNATURE"],
    )


async def test_get_raises_on_a_non_2xx_response(signer: KalshiRequestSigner) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"error": "unauthorized"}, status=401)

    app = web.Application()
    app.router.add_get("/trade-api/v2/portfolio/balance", handler)
    client, server = await _client_for(app, signer)
    try:
        with pytest.raises(ClientResponseError):
            await client.get("/portfolio/balance")
    finally:
        await client.close()
        await server.close()


async def test_error_response_body_is_surfaced_in_the_raised_exception(
    signer: KalshiRequestSigner,
) -> None:
    """`raise_for_status()` alone discards the body. Kalshi's real error code/message (e.g.
    `user_not_found`) must survive into the exception alongside the generic status/reason.
    """

    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {"error": {"code": "user_not_found", "message": "user not found"}}, status=404
        )

    app = web.Application()
    app.router.add_post("/trade-api/v2/portfolio/events/orders", handler)
    client, server = await _client_for(app, signer)
    try:
        with pytest.raises(ClientResponseError) as exc_info:
            await client.post("/portfolio/events/orders", {"ticker": "INXD-24"})
    finally:
        await client.close()
        await server.close()

    assert exc_info.value.status == 404
    assert "user_not_found" in str(exc_info.value)
    assert "user not found" in str(exc_info.value)


async def test_non_json_error_body_is_surfaced_as_text(signer: KalshiRequestSigner) -> None:
    """A route that doesn't exist at all returns a plain-text body instead of Kalshi's JSON
    shape, and that text should still make it into the exception instead of being silently dropped.
    """

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="404 page not found", status=404)

    app = web.Application()
    app.router.add_get("/trade-api/v2/bogus-route", handler)
    client, server = await _client_for(app, signer)
    try:
        with pytest.raises(ClientResponseError) as exc_info:
            await client.get("/bogus-route")
    finally:
        await client.close()
        await server.close()

    assert "404 page not found" in str(exc_info.value)


async def test_prewarm_creates_the_session_before_any_request(
    signer: KalshiRequestSigner,
) -> None:
    app = web.Application()
    client, server = await _client_for(app, signer)
    try:
        assert client._session is None
        await client.prewarm()
        assert client._session is not None
        assert not client._session.closed
    finally:
        await client.close()
        await server.close()


async def test_close_is_idempotent_including_before_any_request(
    signer: KalshiRequestSigner,
) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/trade-api/v2/exchange/status", handler)
    client, server = await _client_for(app, signer)
    try:
        await client.close()  # never opened a session, so it must not raise
        await client.get("/exchange/status")
    finally:
        await client.close()
        await client.close()
        await server.close()


async def test_session_carries_a_bounded_default_timeout(
    signer: KalshiRequestSigner,
) -> None:
    # aiohttp's own default is 5 minutes, which on the order path is indistinguishable from a
    # hang. This asserts the session is not left on that default.
    client = KalshiRestClient("http://127.0.0.1:1", "key-id", signer)
    try:
        await client.prewarm()
        assert client._session is not None
        assert client._session.timeout.total == rest_client_module.DEFAULT_TIMEOUT.total
        assert client._session.timeout.total is not None
        assert client._session.timeout.total <= 30
    finally:
        await client.close()


def test_order_timeout_is_tighter_than_the_default() -> None:
    assert rest_client_module.ORDER_TIMEOUT.total is not None
    assert rest_client_module.DEFAULT_TIMEOUT.total is not None
    assert rest_client_module.ORDER_TIMEOUT.total < rest_client_module.DEFAULT_TIMEOUT.total


async def test_a_slow_response_raises_rather_than_hanging(
    signer: KalshiRequestSigner,
) -> None:
    # The whole point of the deadline: a server that never answers must not stall the caller.
    async def _never_answers(request: web.Request) -> web.Response:
        await asyncio.sleep(30)
        return web.json_response({})  # pragma: no cover - the timeout fires first

    app = web.Application()
    app.router.add_get("/trade-api/v2/slow", _never_answers)
    server = TestServer(app)
    await server.start_server()
    client = KalshiRestClient(str(server.make_url("")).rstrip("/"), "key-id", signer)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await client.get("/slow", timeout=aiohttp.ClientTimeout(total=0.2))
    finally:
        await client.close()
        await server.close()


async def test_a_per_request_timeout_overrides_the_session_default(
    signer: KalshiRequestSigner,
) -> None:
    async def _slow(request: web.Request) -> web.Response:
        await asyncio.sleep(0.3)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/trade-api/v2/slowish", _slow)
    server = TestServer(app)
    await server.start_server()
    client = KalshiRestClient(str(server.make_url("")).rstrip("/"), "key-id", signer)
    try:
        # Well under the session default of 10s, so only an honored override can fail this.
        with pytest.raises(asyncio.TimeoutError):
            await client.get("/slowish", timeout=aiohttp.ClientTimeout(total=0.05))
        # And a generous override still succeeds against the same endpoint.
        assert await client.get("/slowish", timeout=aiohttp.ClientTimeout(total=5.0)) == {
            "ok": True
        }
    finally:
        await client.close()
        await server.close()


async def test_post_fills_in_request_timings(signer: KalshiRequestSigner) -> None:
    """Every stamp is populated and they are ordered, which is what makes the spans non-negative."""

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"order_id": "abc123"}, status=201)

    app = web.Application()
    app.router.add_post("/trade-api/v2/portfolio/orders", handler)
    client, server = await _client_for(app, signer)
    timings = RequestTimings()
    try:
        await client.post("/portfolio/orders", {"ticker": "INXD-24"}, timings=timings)
    finally:
        await client.close()
        await server.close()

    assert 0 < timings.sign_start_ns <= timings.sign_end_ns <= timings.sent_ns <= timings.ack_ns
    assert timings.sign_ms is not None and timings.sign_ms >= 0.0
    assert timings.dispatch_send_ms is not None and timings.dispatch_send_ms >= 0.0
    assert timings.dispatch_ack_ms is not None and timings.dispatch_ack_ms >= 0.0


async def test_request_timings_survive_a_failed_request(signer: KalshiRequestSigner) -> None:
    """The out-parameter shape earns its keep here: a raise leaves no return value to read.

    A timed-out or rejected order is the case where the timings matter most, so everything
    measured before the failure has to come back anyway.
    """

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"error": {"code": "nope", "message": "no"}}, status=400)

    app = web.Application()
    app.router.add_post("/trade-api/v2/portfolio/orders", handler)
    client, server = await _client_for(app, signer)
    timings = RequestTimings()
    try:
        with pytest.raises(aiohttp.ClientResponseError):
            await client.post("/portfolio/orders", {"ticker": "INXD-24"}, timings=timings)
    finally:
        await client.close()
        await server.close()

    assert timings.sign_ms is not None
    assert timings.sent_ns > 0


async def test_dry_run_signs_but_sends_nothing(signer: KalshiRequestSigner) -> None:
    """A shadow fire must traverse the real path and stop at the socket.

    If it skipped signing it would be measuring a different, cheaper path than the one the
    reported latency claims to describe, so the assertion is both that the server saw nothing
    *and* that the signature was computed.
    """
    received: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        received.append(request.path)
        return web.json_response({"order_id": "should-not-happen"}, status=201)

    app = web.Application()
    app.router.add_post("/trade-api/v2/portfolio/orders", handler)
    client, server = await _client_for(app, signer)
    timings = RequestTimings()
    try:
        result = await client.post(
            "/portfolio/orders", {"ticker": "INXD-24"}, timings=timings, dry_run=True
        )
    finally:
        await client.close()
        await server.close()

    assert result == {}
    assert received == []
    assert timings.sign_ms is not None
    assert timings.sent_ns > 0
    assert timings.ack_ns == 0  # nothing came back, because nothing went out


def test_incomplete_timings_report_none_rather_than_zero() -> None:
    """A span that never closed is unknown, not instantaneous."""
    timings = RequestTimings(sign_start_ns=100, sign_end_ns=200)

    assert timings.sign_ms == pytest.approx(0.0001)
    assert timings.dispatch_send_ms is None
    assert timings.dispatch_ack_ms is None


async def test_delete_signs_the_request_and_returns_the_parsed_json_body(
    signer: KalshiRequestSigner, public_key: rsa.RSAPublicKey
) -> None:
    """`DELETE /portfolio/events/orders/{order_id}` is how a resting maker quote is withdrawn."""
    captured: dict[str, Any] = {}

    async def handler(request: web.Request) -> web.Response:
        captured["path"] = request.path
        captured["query_string"] = request.query_string
        captured["headers"] = dict(request.headers)
        return web.json_response({"order_id": "ko-1", "reduced_by": "1.00", "ts_ms": 1})

    app = web.Application()
    app.router.add_delete("/trade-api/v2/portfolio/events/orders/{order_id}", handler)
    client, server = await _client_for(app, signer)
    try:
        result = await client.delete(
            "/portfolio/events/orders/ko-1", params={"exchange_index": "2"}
        )
    finally:
        await client.close()
        await server.close()

    assert result == {"order_id": "ko-1", "reduced_by": "1.00", "ts_ms": 1}
    assert captured["path"] == "/trade-api/v2/portfolio/events/orders/ko-1"
    assert captured["query_string"] == "exchange_index=2"
    headers = captured["headers"]
    assert headers["KALSHI-ACCESS-KEY"] == _API_KEY_ID
    # Same signing rule as GET: the query string is excluded from the signed message.
    _verify_signature(
        public_key,
        f"{headers['KALSHI-ACCESS-TIMESTAMP']}DELETE/trade-api/v2/portfolio/events/orders/ko-1",
        headers["KALSHI-ACCESS-SIGNATURE"],
    )


async def test_delete_raises_with_the_kalshi_error_code_on_a_non_2xx_response(
    signer: KalshiRequestSigner,
) -> None:
    """Cancelling an order that is already gone returns `404`; the code must survive the raise."""

    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {"error": {"code": "order_not_found", "message": "no such order"}}, status=404
        )

    app = web.Application()
    app.router.add_delete("/trade-api/v2/portfolio/events/orders/{order_id}", handler)
    client, server = await _client_for(app, signer)
    try:
        with pytest.raises(ClientResponseError) as excinfo:
            await client.delete("/portfolio/events/orders/gone")
    finally:
        await client.close()
        await server.close()

    assert excinfo.value.status == 404
    assert "order_not_found" in str(excinfo.value)


async def test_delete_meters_against_the_write_bucket(signer: KalshiRequestSigner) -> None:
    """A cancel is a write: it must not draw from the read budget the pollers depend on."""
    drawn: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({})

    app = web.Application()
    app.router.add_delete("/trade-api/v2/portfolio/events/orders/{order_id}", handler)
    client, server = await _client_for(app, signer)

    class _RecordingLimiter:
        async def acquire(self, method: str) -> float:
            drawn.append(method)
            return 0.0

    client.rate_limiter = _RecordingLimiter()  # type: ignore[assignment]
    try:
        await client.delete("/portfolio/events/orders/ko-1")
    finally:
        await client.close()
        await server.close()

    assert drawn == ["DELETE"]


async def test_the_client_recovers_after_its_session_is_closed(
    signer: KalshiRequestSigner,
) -> None:
    """A closed session must not end the client, which is long-lived in the executor.

    `close()` is called on shutdown paths and can also be reached by a supervisor tearing down a
    connector mid-life. `_session_or_create()` is written to rebuild lazily, but nothing pinned
    it: a regression that cached a closed session would surface as a dead executor after the
    first transient teardown, at which point every subsequent fire fails for a reason that looks
    nothing like its cause. This is the order path's "reconnect" case.
    """
    calls: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        calls.append(request.path)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/trade-api/v2/exchange/status", handler)
    client, server = await _client_for(app, signer)
    try:
        assert await client.get("/exchange/status") == {"ok": True}
        await client.close()
        # Same client object, session gone. The next request must rebuild it, not raise.
        assert await client.get("/exchange/status") == {"ok": True}
    finally:
        await client.close()
        await server.close()

    assert len(calls) == 2


async def test_prewarm_after_close_rebuilds_the_session(signer: KalshiRequestSigner) -> None:
    """The executor prewarms at startup; a restart-in-place must be able to prewarm again."""

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/trade-api/v2/exchange/status", handler)
    client, server = await _client_for(app, signer)
    try:
        await client.prewarm()
        await client.close()
        await client.prewarm()
        assert await client.get("/exchange/status") == {}
    finally:
        await client.close()
        await server.close()
