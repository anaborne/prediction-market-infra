"""Client-side rate limiting against Kalshi's documented token budgets.

Kalshi's Basic tier allows 200 read tokens/s and 100 write tokens/s at a default cost of 10
tokens per request, roughly 20 reads/s and 10 writes/s, and, critically, its `429` responses
carry no `Retry-After` and no `X-RateLimit-*` headers: there is nothing server-side to react
to, so a client must track its own budget or discover the limit by losing requests. This module
is that tracking. Verified against Kalshi's published rate-limit documentation
(`docs.kalshi.com`) on 2026-08-21.

Read and write budgets are separate buckets, matching how Kalshi meters them. One
`KalshiRateLimiter` is shared per `KalshiRestClient` (which every caller in a process shares),
so each asset's ladder poller, the balance checks, and the order path all draw from one budget.
The failure the limiter exists to prevent is precisely independent callers each assuming they
have the whole budget.

`acquire()` is arithmetic when tokens are available, the common case by a wide margin, since
this bot's steady-state request rate (two ladder fetches a minute plus a few fires an hour) sits
far below even the Basic tier. The waiting branch exists as armor for bursts and bugs, not as an
active throttle.
"""

from __future__ import annotations

import asyncio
from typing import Final

from kalshi_bot.runtime.clock import monotonic_ns

# Kalshi Basic tier. Deliberately not configurable per-request: a caller that wants more budget
# should upgrade the account tier, not edit the constant that models the server's behavior.
READ_TOKENS_PER_SECOND: Final = 200.0
WRITE_TOKENS_PER_SECOND: Final = 100.0
TOKENS_PER_REQUEST: Final = 10.0

# Bucket capacity, in seconds of budget. Kalshi documents burst as "one to two seconds of
# budget depending on tier"; one second is the conservative reading.
_BURST_SECONDS: Final = 1.0


class TokenBucket:
    """A monotonic-clock token bucket for one budget.

    Attributes:
        rate_per_second: Refill rate in tokens per second.
        capacity: Maximum tokens the bucket holds (the burst allowance).
    """

    def __init__(self, rate_per_second: float, capacity: float) -> None:
        """Start full, since a fresh process has its whole burst allowance available.

        Args:
            rate_per_second: Refill rate in tokens per second.
            capacity: Maximum tokens the bucket holds.
        """
        self.rate_per_second = rate_per_second
        self.capacity = capacity
        self._tokens = capacity
        self._refilled_at_ns = monotonic_ns()
        # Serializes token accounting; also queues waiters fairly (asyncio.Lock is FIFO), so a
        # burst drains in arrival order instead of racing.
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = TOKENS_PER_REQUEST) -> float:
        """Take `tokens`, waiting for refill if the bucket cannot cover them now.

        Args:
            tokens: Cost to withdraw.

        Returns:
            Seconds spent waiting (`0.0` on the untouched fast path), for callers that want to
            log or measure throttling.
        """
        async with self._lock:
            self._refill()
            waited = 0.0
            if self._tokens < tokens:
                deficit = tokens - self._tokens
                waited = deficit / self.rate_per_second
                # Sleep inside the lock: the next waiter's wait time depends on this one having
                # actually withdrawn, and FIFO fairness is the point of queuing here.
                await asyncio.sleep(waited)
                self._refill()
            self._tokens = max(0.0, self._tokens - tokens)
            return waited

    def _refill(self) -> None:
        now_ns = monotonic_ns()
        elapsed_s = (now_ns - self._refilled_at_ns) / 1_000_000_000
        self._refilled_at_ns = now_ns
        self._tokens = min(self.capacity, self._tokens + elapsed_s * self.rate_per_second)


class KalshiRateLimiter:
    """Paired read/write buckets matching Kalshi's metering.

    Attributes:
        read_bucket: Budget drawn by GET requests.
        write_bucket: Budget drawn by POST/DELETE requests.
    """

    def __init__(
        self,
        read_tokens_per_second: float = READ_TOKENS_PER_SECOND,
        write_tokens_per_second: float = WRITE_TOKENS_PER_SECOND,
    ) -> None:
        """Build both buckets, each with one second of burst capacity.

        Args:
            read_tokens_per_second: Read budget refill rate.
            write_tokens_per_second: Write budget refill rate.
        """
        self.read_bucket = TokenBucket(
            read_tokens_per_second, read_tokens_per_second * _BURST_SECONDS
        )
        self.write_bucket = TokenBucket(
            write_tokens_per_second, write_tokens_per_second * _BURST_SECONDS
        )

    async def acquire(self, method: str) -> float:
        """Draw one request's cost from the bucket `method` meters against.

        Args:
            method: HTTP method; GET draws from the read budget, everything else from write.

        Returns:
            Seconds spent waiting, `0.0` on the fast path.
        """
        bucket = self.read_bucket if method.upper() == "GET" else self.write_bucket
        return await bucket.acquire()
