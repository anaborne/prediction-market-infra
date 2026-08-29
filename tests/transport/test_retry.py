"""Tests for `transport.retry`: what retries, what must never retry, and the give-up path."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from kalshi_bot.transport.retry import is_retryable, with_retries


def _response_error(status: int) -> aiohttp.ClientResponseError:
    request_info = aiohttp.RequestInfo(
        url=URL("https://example.test/x"),
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL("https://example.test/x"),
    )
    return aiohttp.ClientResponseError(request_info, (), status=status, message="test")


def test_429_and_5xx_are_retryable() -> None:
    assert is_retryable(_response_error(429))
    assert is_retryable(_response_error(500))
    assert is_retryable(_response_error(503))


def test_4xx_besides_429_is_not_retryable() -> None:
    """A 404/409 is a real answer about this request, and retrying cannot change it."""
    assert not is_retryable(_response_error(404))
    assert not is_retryable(_response_error(409))  # fill_or_kill_insufficient_resting_volume


def test_connection_failures_are_retryable_but_timeouts_never() -> None:
    """The load-bearing line: a timed-out order may be resting on Kalshi's book right now.

    `ServerTimeoutError` subclasses `ClientConnectionError`, which is why it needs (and has) an
    explicit exclusion. Inheriting retryability from its parent here would be exactly the
    double-buy this module exists to prevent.
    """
    assert is_retryable(aiohttp.ClientConnectionError("refused"))
    assert not is_retryable(TimeoutError())
    assert not is_retryable(aiohttp.ServerTimeoutError("timed out"))


async def test_succeeds_after_a_retryable_failure() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _response_error(503)
        return "ok"

    result = await with_retries(flaky, label="GET /flaky")

    assert result == "ok"
    assert calls == 3


async def test_gives_up_after_the_final_attempt(caplog: pytest.LogCaptureFixture) -> None:
    calls = 0

    async def always_503() -> Any:
        nonlocal calls
        calls += 1
        raise _response_error(503)

    with caplog.at_level("WARNING"), pytest.raises(aiohttp.ClientResponseError):
        await with_retries(always_503, label="GET /down", attempts=3)

    assert calls == 3
    assert "retrying" in caplog.text


async def test_non_retryable_raises_immediately() -> None:
    calls = 0

    async def timeout() -> Any:
        nonlocal calls
        calls += 1
        raise TimeoutError

    with pytest.raises(asyncio.TimeoutError):
        await with_retries(timeout, label="POST /portfolio/events/orders")

    assert calls == 1  # ambiguous failures get exactly one attempt, ever
