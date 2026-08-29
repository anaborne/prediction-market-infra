"""Tests for `transport.rate_limit`.

Timing-sensitive assertions use generous margins: the point under test is "waited roughly the
deficit" versus "did not wait at all", never exact durations.
"""

from __future__ import annotations

import asyncio
import time

from kalshi_bot.transport.rate_limit import KalshiRateLimiter, TokenBucket


async def test_burst_within_capacity_never_waits() -> None:
    bucket = TokenBucket(rate_per_second=100.0, capacity=100.0)

    waits = [await bucket.acquire(10.0) for _ in range(10)]

    assert waits == [0.0] * 10


async def test_exhausted_bucket_waits_for_refill() -> None:
    bucket = TokenBucket(rate_per_second=1000.0, capacity=10.0)
    assert await bucket.acquire(10.0) == 0.0  # drain the burst

    start = time.perf_counter()
    waited = await bucket.acquire(10.0)  # needs 10 tokens at 1000/s -> ~10 ms
    elapsed = time.perf_counter() - start

    assert waited > 0.0
    assert 0.005 < elapsed < 0.5


async def test_refill_is_capped_at_capacity() -> None:
    bucket = TokenBucket(rate_per_second=1_000_000.0, capacity=20.0)
    await bucket.acquire(10.0)

    # 10 ms at a million tokens/s would refill 10,000 tokens if uncapped.
    await asyncio.sleep(0.01)
    bucket._refill()

    assert bucket._tokens == 20.0


async def test_limiter_routes_methods_to_separate_budgets() -> None:
    limiter = KalshiRateLimiter(read_tokens_per_second=1000.0, write_tokens_per_second=1000.0)

    await limiter.acquire("GET")
    await limiter.acquire("POST")

    # One request's cost (10) gone from each bucket, independently.
    assert limiter.read_bucket._tokens == limiter.write_bucket._tokens
    assert limiter.read_bucket._tokens < 1000.0
