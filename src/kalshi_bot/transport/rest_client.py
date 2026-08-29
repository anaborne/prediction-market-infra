"""REST client for the Kalshi API.

Wraps an HTTP session for request/response calls against a Kalshi base URL, signing outbound
requests via `kalshi_bot.auth.signer.KalshiRequestSigner`.

`base_url` (from `KalshiBotConfig.demo_base_url`/`prod_base_url`) is the API host only, e.g.
`https://external-api.demo.kalshi.co`, and it does not include the `/trade-api/v2` prefix. This
client owns that prefix (`_API_PREFIX` below) and prepends it to every caller-supplied `path` to
build both the request URL and the path signed per `ENGINEERING.md`'s "Kalshi API v2 auth spec", so
callers pass paths like `/portfolio/orders`, not `/trade-api/v2/portfolio/orders`.

Every request has a deadline. `aiohttp`'s default is a 5-minute total timeout, which on the
order path is indistinguishable from a hang: a fill-or-kill order that has not resolved in
seconds has already missed the edge it was placed for, and the executor would sit on it while the
market moved. The session carries a conservative default and each call may tighten it; see
`DEFAULT_TIMEOUT` and `ORDER_TIMEOUT`.

Note what a timeout does *not* tell you. A request that times out mid-flight may still have been
received and acted on by Kalshi, so a timed-out order is ambiguous, not failed, and must not
be retried blindly. See `execution/order_dispatcher.py`.

Latency instrumentation is reported through the `RequestTimings` out-parameter rather than written
from here. `transport` must not import `telemetry` (`docs/GUIDE.md §7`); `execution` owns the
telemetry write for a dispatch, and this module only fills in the numbers it alone can see.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final

import aiohttp
import orjson

from kalshi_bot.auth.signer import KalshiRequestSigner
from kalshi_bot.runtime.clock import monotonic_ns
from kalshi_bot.transport.rate_limit import KalshiRateLimiter
from kalshi_bot.transport.retry import with_retries

_API_PREFIX = "/trade-api/v2"

# Session-wide default. Generous enough for market/event listings, which page through hundreds of
# rows, while still bounded. `aiohttp`'s own default of 5 minutes is not a deadline in any useful
# sense.
DEFAULT_TIMEOUT: Final = aiohttp.ClientTimeout(total=10.0, connect=3.0)

# The order path. A fill-or-kill order unresolved after this has missed its edge, and waiting
# longer only widens the window in which the executor is blind to what happened.
ORDER_TIMEOUT: Final = aiohttp.ClientTimeout(total=2.0, connect=1.0)


@dataclass(slots=True)
class RequestTimings:
    """Monotonic stamps taken inside one `_request()` call, filled in for the caller.

    Passed in empty and mutated in place as an out-parameter, never a return value, so that a
    request which raises still hands back everything measured before the failure. A timed-out
    order is the case that matters most and is exactly the case where there is no return value.

    This exists instead of a `telemetry` import because `transport` importing `telemetry` would
    invert a module boundary `docs/GUIDE.md §7` calls load-bearing. `execution` reads these
    fields and writes the rows.

    All fields are `runtime.clock.monotonic_ns()` readings, `0` if that point was never reached.

    Attributes:
        sign_start_ns: Immediately before the RSA-PSS signature is computed.
        sign_end_ns: Immediately after it returns.
        sent_ns: The instant the request is handed to `aiohttp`, after the body is serialized and
            the headers assembled. A lower bound on when bytes hit the socket: connection
            acquisition from the pool happens inside `aiohttp` and is not visible from here. It is
            the last point this codebase controls, which is what makes it the right end for
            `detect_fire`. See `docs/GUIDE.md`.
        ack_ns: After the response body has been fully read.
    """

    sign_start_ns: int = 0
    sign_end_ns: int = 0
    sent_ns: int = 0
    ack_ns: int = 0

    @property
    def sign_ms(self) -> float | None:
        """Signature computation, in milliseconds, or `None` if it did not complete."""
        return self._span_ms(self.sign_start_ns, self.sign_end_ns)

    @property
    def dispatch_send_ms(self) -> float | None:
        """Post-signature request assembly, in milliseconds, or `None` if never sent."""
        return self._span_ms(self.sign_end_ns, self.sent_ns)

    @property
    def dispatch_ack_ms(self) -> float | None:
        """Network round trip plus body read, in milliseconds, or `None` if no response came."""
        return self._span_ms(self.sent_ns, self.ack_ns)

    @staticmethod
    def _span_ms(start_ns: int, end_ns: int) -> float | None:
        if start_ns == 0 or end_ns == 0:
            return None
        return (end_ns - start_ns) / 1_000_000


class KalshiRestClient:
    """Async REST client for the Kalshi API.

    Attributes:
        base_url: Base URL of the target Kalshi environment (demo or prod), host only.
        api_key_id: Kalshi API key identifier sent in the `KALSHI-ACCESS-KEY` header.
        signer: Request signer used to authenticate outbound requests.
    """

    def __init__(
        self,
        base_url: str,
        api_key_id: str,
        signer: KalshiRequestSigner,
        rate_limiter: KalshiRateLimiter | None = None,
    ) -> None:
        """Store the base URL, key id, and signer for later use by request methods.

        The underlying `aiohttp.ClientSession` is created lazily, on first request, rather than
        here, because `aiohttp` expects session creation to happen inside a running event loop.

        Args:
            base_url: Base URL of the target Kalshi environment, host only (no `/trade-api/v2`).
            api_key_id: Kalshi API key identifier for the `KALSHI-ACCESS-KEY` header.
            signer: Request signer used to authenticate outbound requests.
            rate_limiter: Client-side budget tracker shared by every request through this
                client. Defaults to a fresh `KalshiRateLimiter` at Kalshi's Basic-tier budgets,
                on by default because Kalshi's `429`s carry no headers to react to
                (`transport/rate_limit.py`). Injectable for tests.
        """
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self.signer = signer
        self.rate_limiter = rate_limiter if rate_limiter is not None else KalshiRateLimiter()
        self._session: aiohttp.ClientSession | None = None

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict[str, Any]:
        """Issue a signed GET request.

        Args:
            path: Request path, excluding query string and the `/trade-api/v2` prefix.
            params: Optional query parameters.
            timeout: Overrides the session default for this request.

        Returns:
            Parsed JSON response body.

        Raises:
            asyncio.TimeoutError: If the request does not complete within the deadline.
                Timeouts are never retried (`transport/retry.py`); `429`/`5xx`/connection
                failures are, with a fresh timestamp and signature per attempt.
        """
        return await with_retries(
            lambda: self._request("GET", path, params=params, timeout=timeout),
            label=f"GET {path}",
        )

    async def post(
        self,
        path: str,
        body: dict[str, Any],
        timeout: aiohttp.ClientTimeout | None = None,
        timings: RequestTimings | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Issue a signed POST request.

        Args:
            path: Request path, excluding query string and the `/trade-api/v2` prefix.
            body: JSON-serializable request body.
            timeout: Overrides the session default for this request. `OrderDispatcher` passes
                `ORDER_TIMEOUT` here.
            timings: Filled in with this request's monotonic stamps, including on failure. See
                `RequestTimings`.
            dry_run: Sign and assemble the request but do not send it, returning `{}`. Used by
                the shadow-fire latency measurement; see `RequestTimings.sent_ns`.

        Returns:
            Parsed JSON response body, or `{}` when `dry_run` is set.

        Raises:
            asyncio.TimeoutError: If the request does not complete within the deadline. The
                request may still have been received by Kalshi (see the module docstring),
                which is exactly why timeouts are never retried here. `429`/`5xx`/connection
                failures are: the request demonstrably did not take effect (or, for `5xx`, the
                caller's `client_order_id` makes the duplicate harmless), and each attempt
                re-signs with a fresh timestamp. On a retried request `timings` reflects the
                final attempt.
        """
        if dry_run:
            # A shadow fire cannot fail for a transport reason (nothing is sent), and wrapping
            # it would put a lambda allocation on the measured path for no behavior.
            return await self._request(
                "POST", path, body=body, timeout=timeout, timings=timings, dry_run=True
            )
        return await with_retries(
            lambda: self._request("POST", path, body=body, timeout=timeout, timings=timings),
            label=f"POST {path}",
        )

    async def delete(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
        timings: RequestTimings | None = None,
    ) -> dict[str, Any]:
        """Issue a signed DELETE request.

        Exists for one reason: `DELETE /portfolio/events/orders/{order_id}` (`CancelOrderV2`) is
        how a resting order is withdrawn, and a maker that cannot cancel is a way to leave
        inventory on the book. Meters against the write bucket, like
        POST (`transport/rate_limit.py` sends everything that is not a GET there).

        That is deliberately conservative rather than exact. Read from a real `GET
        /account/endpoint_costs` (2026-08-24): the account's `default_cost` is 10, and `DELETE
        /trade-api/v2/portfolio/events/orders/:order_id` is one of only three order-related paths
        priced below it, at 2. Order *creation* is not listed and so costs the default 10, so a
        cancel is a fifth the price of a place. This client does not model per-endpoint costs, so a
        cancel draws a full write token; that spends more budget than Kalshi charges, never less,
        which is the safe direction to be wrong in.

        Retried on the same terms as POST: `429`/`5xx`/connection failures only, never a
        timeout. A cancel is idempotent in effect (a second cancel of an already-cancelled order
        returns `404`, it does not un-cancel anything), so a retried cancel cannot double-act the
        way a retried order placement could.

        Args:
            path: Request path, excluding query string and the `/trade-api/v2` prefix.
            params: Optional query parameters (`exchange_index`, `market_ticker`).
            timeout: Overrides the session default for this request.
            timings: Filled in with this request's monotonic stamps, including on failure.

        Returns:
            Parsed JSON response body.

        Raises:
            asyncio.TimeoutError: If the request does not complete within the deadline.
        """
        return await with_retries(
            lambda: self._request("DELETE", path, params=params, timeout=timeout, timings=timings),
            label=f"DELETE {path}",
        )

    async def close(self) -> None:
        """Release any underlying network resources (e.g. the HTTP session)."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def prewarm(self) -> None:
        """Force the underlying `aiohttp.ClientSession` to exist ahead of the first request.

        `_session_or_create()` already runs lazily on every request, so this call has no effect on
        request behavior. It only moves session/connector-pool construction earlier for a caller
        (the Phase 8 executor process) that wants that allocation done at process startup, off the
        per-fire hot path, instead of on whichever request happens to be first. Constructing
        `aiohttp.ClientSession()` does not by itself open a TCP connection, since the actual
        connect/TLS handshake to Kalshi still happens lazily on the first real request
        `session.request(...)` makes, so this narrows, without eliminating, the first-request
        latency gap; a true connection-level prewarm would need a real request (e.g. an
        unauthenticated GET), which is not done here. See `docs/GUIDE.md`.
        """
        self._session_or_create()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
        timings: RequestTimings | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        # Budget check before signing, not after: a wait here must not stale the signed
        # timestamp. Shadow fires skip it, since they never reach the network, and drawing write
        # budget for them would throttle real orders on behalf of requests that don't exist.
        if not dry_run:
            await self.rate_limiter.acquire(method)
        signed_path = f"{_API_PREFIX}{path}"
        timestamp = str(int(time.time() * 1000))
        sign_start_ns = monotonic_ns()
        signature = self.signer.sign(timestamp, method, signed_path)
        sign_end_ns = monotonic_ns()
        if timings is not None:
            timings.sign_start_ns = sign_start_ns
            timings.sign_end_ns = sign_end_ns
        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }
        data = orjson.dumps(body) if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"

        # Resolved before the dry-run branch, not after, so both paths stamp `sent_ns` at exactly
        # the same point. Stamping the shadow path one call earlier would make it systematically
        # cheaper than the real one, and since nearly every `detect_fire` row is a shadow fire,
        # the headline number would describe a path one step shorter than the one it names.
        session = self._session_or_create()
        if timings is not None:
            timings.sent_ns = monotonic_ns()

        if dry_run:
            # Everything above ran for real: the signature was computed, the body serialized,
            # the headers built, the session resolved. Only the network call is skipped. That is
            # the point: a shadow fire has to traverse the identical code path, or the latency it
            # measures is a different path's latency. See
            # `docs/GUIDE.md`.
            return {}

        async with session.request(
            method,
            f"{self.base_url}{signed_path}",
            params=params,
            data=data,
            headers=headers,
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
        ) as response:
            payload = await response.read()
            if timings is not None:
                timings.ack_ns = monotonic_ns()
            if response.status >= 400:
                # Read the body before raising. Kalshi's error responses are a JSON
                # {"error": {"code": ..., "message": ...}} body (e.g. "user_not_found",
                # "fill_or_kill_insufficient_resting_volume"), which `raise_for_status()` alone
                # would discard (it raises before the body is ever read), leaving every caller
                # (including `execution.OrderDispatcher.dispatch()`'s `error_message` telemetry
                # column) with only aiohttp's generic "404, message='Not Found'" and no way to
                # tell one 4xx cause from another.
                raise aiohttp.ClientResponseError(
                    response.request_info,
                    response.history,
                    status=response.status,
                    message=_error_message(response, payload),
                    headers=response.headers,
                )

        if not payload:
            return {}
        parsed: dict[str, Any] = orjson.loads(payload)
        return parsed

    def _session_or_create(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
        return self._session


def _error_message(response: aiohttp.ClientResponse, payload: bytes) -> str:
    """Best-effort human-readable message for a non-2xx response, from its body.

    Kalshi's own error responses are `{"error": {"code": ..., "message": ...}}`; a route that
    doesn't exist at all (nothing to do with this client) returns a plain-text body instead
    (`"404 page not found"`). Both are surfaced here instead of only the status code.
    """
    try:
        parsed: Any = orjson.loads(payload)
    except orjson.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
        error = parsed["error"]
        return f"{response.status} {error.get('code')}: {error.get('message')}"
    text = payload.decode("utf-8", errors="replace").strip()
    if text:
        return f"{response.status} {response.reason}: {text}"
    return f"{response.status} {response.reason}"
