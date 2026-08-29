"""Retry policy for transient transport failures, and, deliberately, nothing ambiguous.

The line this module draws:

- Retryable: `429` (rate limited, so the request was refused and not processed), `5xx` (Kalshi's
  demo returns `503 service_unavailable` intermittently, observed on the order path), and
  connection-level failures (`aiohttp.ClientConnectionError`: DNS, refused, reset before the
  request went out). In every one of these the request demonstrably did not take effect, or in
  the `5xx` case the caller's idempotency key (`client_order_id`, enforced UNIQUE by Kalshi)
  makes a duplicate submission harmless.
- Never retryable: timeouts. A timed-out request is ambiguous and not failed, since the order
  may be resting on Kalshi's book right now (`transport/rest_client.py`'s module docstring).
  Retrying it blindly is how a bot double-buys. Callers who *know* a retry is safe pass the same
  `client_order_id` back in explicitly; this module will not decide that for them.

Each attempt re-runs the whole operation, which for `KalshiRestClient` means a fresh timestamp
and a fresh signature, so a retried request is never sent with a stale auth window.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Final

import aiohttp

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES: Final = frozenset({429, 500, 502, 503, 504})

ATTEMPTS: Final = 3
_BASE_DELAY_SECONDS: Final = 0.25
_MAX_DELAY_SECONDS: Final = 2.0


def is_retryable(exc: BaseException) -> bool:
    """Whether a failure is safe and worthwhile to retry. Timeouts are neither, see module doc."""
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status in _RETRYABLE_STATUSES
    # Order matters: ClientConnectionError before the ClientError umbrella, TimeoutError checked
    # nowhere because it must fall through to False. `asyncio.TimeoutError` is not a
    # `ClientConnectionError`, but `ServerTimeoutError` is, so exclude it explicitly.
    if isinstance(exc, aiohttp.ServerTimeoutError):
        return False
    return isinstance(exc, aiohttp.ClientConnectionError)


async def with_retries[T](
    operation: Callable[[], Awaitable[T]],
    *,
    label: str,
    attempts: int = ATTEMPTS,
    retryable: Callable[[BaseException], bool] = is_retryable,
) -> T:
    """Run `operation`, retrying transient failures with jittered exponential backoff.

    Args:
        operation: Zero-argument coroutine factory; called fresh per attempt so signatures and
            timestamps are rebuilt.
        label: Short description for the retry log line, e.g. `"GET /events"`.
        attempts: Total tries, including the first.
        retryable: Predicate deciding whether a failure may be retried.

    Returns:
        The first successful attempt's result.

    Raises:
        BaseException: The final attempt's failure, or the first non-retryable one, unchanged.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except BaseException as exc:
            if attempt >= attempts or not retryable(exc):
                raise
            delay = min(_MAX_DELAY_SECONDS, _BASE_DELAY_SECONDS * 2 ** (attempt - 1))
            delay *= 0.5 + random.random() / 2  # nosec B311 - jitter, not cryptography
            logger.warning(
                "%s failed (%s: %s); retrying in %.2fs (attempt %d/%d)",
                label,
                type(exc).__name__,
                exc,
                delay,
                attempt,
                attempts,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable: the loop either returns or raises")
