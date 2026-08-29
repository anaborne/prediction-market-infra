"""Phase 9 latency benchmark suite.

Standalone, outside the pytest suite (same pattern as `scripts/live_*.py`). Run directly via
`uv run python benchmarks/latency_bench.py`, which also
appends the run's results to `benchmarks/history.csv`.

Measures the two hot-path contributors this codebase can currently exercise without a live
network call:

1. Auth: `KalshiRequestSigner.sign()` wall time, against an ephemeral (never-registered,
   throwaway) RSA-2048 keypair generated for this run only, so no real credentials are needed.
2. Poller -> executor wake: a real `IPCPollerClient` -> real Unix domain socket -> real
   `ExecutorServer` -> real `OrderDispatcher` round trip, against a fake `KalshiRestClient` (no live
   network), matching `tests/ipc/test_wake_round_trip.py`'s pattern. Run once under stock `asyncio`
   and once under `uvloop`, to produce the comparison that justifies (or would have ruled out)
   `uvloop` adoption per `ENGINEERING.md`'s "adopt once transport has a working implementation to
   benchmark against stock asyncio" gate. See `docs/GUIDE.md`.

Deliberately does not attempt to measure the full detect -> fire path end-to-end: that
requires a real signed round trip to Kalshi's demo API, which (a) depends on network conditions
this script can't control or reproduce, and (b) needs a demo account and credentials this
repository does not ship. Reporting a fabricated end-to-end
number would misrepresent reality worse than reporting none, so this script sums the two
contributors it *can* measure into a labeled partial estimate and leaves the rest to
`docs/GUIDE.md §7.2`'s notes.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import math
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvloop
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.auth.signer import KalshiRequestSigner
from kalshi_bot.execution.order_dispatcher import OrderDispatcher, permit_orders
from kalshi_bot.ipc.executor_server import ExecutorServer
from kalshi_bot.ipc.poller_client import IPCPollerClient
from kalshi_bot.ipc.protocol import SCHEMA_VERSION, WakeMessage
from kalshi_bot.telemetry.db import TelemetryDB

_SIGN_ITERATIONS = 2000
_WAKE_ITERATIONS = 300

_HISTORY_CSV = Path(__file__).parent / "history.csv"


def _platform_tag() -> str:
    """A coarse machine label, written into every history row.

    Latency is a property of the machine, not of the code, so two rows are only comparable if
    they came from the same one. Before this column existed the CSV silently invited exactly
    that comparison. It is deliberately coarse (OS, architecture, and interpreter version, with
    no hostname) because the point is to tell an Apple-silicon laptop apart from an x86 CI
    runner, not to identify whose machine produced a row.
    """
    return f"{platform.system()}-{platform.machine()}-py{platform.python_version()}"


_HISTORY_FIELDS = [
    "timestamp_utc",
    "platform",
    "sign_p50_ms",
    "sign_p99_ms",
    "wake_send_asyncio_p50_ms",
    "wake_send_asyncio_p99_ms",
    "wake_recv_asyncio_p50_ms",
    "wake_recv_asyncio_p99_ms",
    "wake_send_uvloop_p50_ms",
    "wake_send_uvloop_p99_ms",
    "wake_recv_uvloop_p50_ms",
    "wake_recv_uvloop_p99_ms",
    "partial_detect_to_fire_p99_ms_estimate",
]


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile, `p` in [0, 1]. `statistics.quantiles` needs n>=2."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return ordered[int(rank)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


@dataclass(frozen=True)
class Stats:
    p50_ms: float
    p99_ms: float
    mean_ms: float
    n: int

    @classmethod
    def from_samples(cls, samples: list[float]) -> Stats:
        return cls(
            p50_ms=_percentile(samples, 0.50),
            p99_ms=_percentile(samples, 0.99),
            mean_ms=statistics.mean(samples),
            n=len(samples),
        )


def _report_line(label: str, stats: Stats, target_ms: float) -> str:
    verdict = "OK" if stats.p99_ms < target_ms else "OVER BUDGET"
    return (
        f"{label:<32} p50={stats.p50_ms:7.3f}ms  p99={stats.p99_ms:7.3f}ms  "
        f"mean={stats.mean_ms:7.3f}ms  n={stats.n:<5}  target=<{target_ms}ms  [{verdict}]"
    )


def bench_sign(iterations: int = _SIGN_ITERATIONS) -> Stats:
    """Benchmark `KalshiRequestSigner.sign()` against a throwaway RSA-2048 keypair."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "bench_key.pem"
        key_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        signer = KalshiRequestSigner(key_path)

        durations_ms: list[float] = []
        for _ in range(iterations):
            timestamp = str(int(time.time() * 1000))
            start_ns = time.perf_counter_ns()
            signer.sign(timestamp, "GET", "/trade-api/v2/portfolio/orders")
            durations_ms.append((time.perf_counter_ns() - start_ns) / 1_000_000)

    return Stats.from_samples(durations_ms)


class _FakeRestClient:
    """No live network. Mirrors `tests/ipc/test_wake_round_trip.py`'s fake.

    The signature must keep accepting whatever keyword arguments `OrderDispatcher.dispatch()`
    passes the real client (`timeout`/`timings`/`dry_run` today). When it fell behind, every
    dispatch died with a `TypeError` that `_handle_fire` caught and logged, `posted` never
    advanced, and the whole benchmark timed out, with nothing pointing at this class.
    """

    async def post(self, path: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {"order_id": "bench-order", "fill_count": "1.00", "remaining_count": "0.00"}


async def _wait_until(predicate: Callable[[], bool], timeout_s: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("condition not met before timeout")
        await asyncio.sleep(0.005)


async def _run_wake_roundtrip(
    iterations: int, telemetry_db_path: Path, socket_path: Path
) -> tuple[list[float], list[float]]:
    poller_db = TelemetryDB(telemetry_db_path)
    poller_db.initialize()
    executor_db = TelemetryDB(telemetry_db_path)
    executor_db.initialize()

    rest_client = _FakeRestClient()
    dispatcher = OrderDispatcher(rest_client, executor_db, permit_orders)  # type: ignore[arg-type]
    server = ExecutorServer(
        socket_path=socket_path,
        dispatcher=dispatcher,
        telemetry_db=executor_db,
        fixed_order_contract_count=1,
        order_time_in_force="fill_or_kill",
        order_self_trade_prevention_type="taker_at_cross",
        kill_switch=None,
        risk_gate=None,
        balance_cache=None,
    )
    server_task = asyncio.create_task(server.serve_forever())
    await _wait_until(lambda: server._server is not None, timeout_s=5.0)

    client = IPCPollerClient(socket_path, poller_db)
    client.start()

    posted = 0
    orig_post = rest_client.post

    async def _counting_post(path: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        nonlocal posted
        result = await orig_post(path, body, **kwargs)
        posted += 1
        return result

    rest_client.post = _counting_post  # type: ignore[method-assign]

    try:
        for i in range(iterations):
            client.send_wake(
                WakeMessage(
                    schema_version=SCHEMA_VERSION,
                    correlation_id=f"bench-{i}",
                    market_ticker="KXBENCH-T100",
                    asset="BENCH",
                    direction="yes",
                    kalshi_price=0.5,
                    wire_price_yes_dollars=0.5,
                    model_probability=0.55,
                    fee=0.01,
                    edge=0.04,
                    decision_ts_ms=0,
                    sent_at_ms=int(time.time() * 1000),
                    sent_at_ns=time.perf_counter_ns(),
                )
            )
        await _wait_until(lambda: posted >= iterations, timeout_s=30.0)
    finally:
        await client.close()
        await server.close()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        poller_db.close()
        executor_db.close()

    conn = sqlite3.connect(telemetry_db_path)
    try:
        wake_send = [
            row[0]
            for row in conn.execute(
                "SELECT duration_ms FROM latency_events WHERE stage = 'wake_send'"
            )
        ]
        wake_recv = [
            row[0]
            for row in conn.execute(
                "SELECT duration_ms FROM latency_events WHERE stage = 'wake_recv'"
            )
        ]
    finally:
        conn.close()
    return wake_send, wake_recv


def bench_wake_roundtrip(
    run: Callable[..., Any], iterations: int = _WAKE_ITERATIONS
) -> tuple[Stats, Stats]:
    """Run the wake round trip under whichever loop runner `run` provides.

    `run` is `asyncio.run` or `uvloop.run`. Uses a `/tmp`-rooted socket dir, same as
    `tests/ipc/`, because `AF_UNIX` paths have a short OS-level length limit that a
    `pytest`-generated `tmp_path` can exceed.
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        socket_path = Path(tmp) / "executor.sock"
        db_path = Path(tmp) / "telemetry.sqlite"
        wake_send, wake_recv = run(_run_wake_roundtrip(iterations, db_path, socket_path))
    return Stats.from_samples(wake_send), Stats.from_samples(wake_recv)


def main() -> int:
    print(f"Signature computation: {_SIGN_ITERATIONS} iterations, RSA-2048 PSS...")
    sign_stats = bench_sign()
    print(_report_line("sign()", sign_stats, target_ms=3.0))

    print(f"\nPoller->executor wake round trip: {_WAKE_ITERATIONS} messages, stock asyncio...")
    wake_send_asyncio, wake_recv_asyncio = bench_wake_roundtrip(asyncio.run)
    print(_report_line("wake_send (asyncio)", wake_send_asyncio, target_ms=1.0))
    print(_report_line("wake_recv (asyncio)", wake_recv_asyncio, target_ms=1.0))

    print(f"\nPoller->executor wake round trip: {_WAKE_ITERATIONS} messages, uvloop...")
    wake_send_uvloop, wake_recv_uvloop = bench_wake_roundtrip(uvloop.run)
    print(_report_line("wake_send (uvloop)", wake_send_uvloop, target_ms=1.0))
    print(_report_line("wake_recv (uvloop)", wake_recv_uvloop, target_ms=1.0))

    send_delta = (
        (wake_send_asyncio.p99_ms - wake_send_uvloop.p99_ms) / wake_send_asyncio.p99_ms * 100
        if wake_send_asyncio.p99_ms
        else 0.0
    )
    recv_delta = (
        (wake_recv_asyncio.p99_ms - wake_recv_uvloop.p99_ms) / wake_recv_asyncio.p99_ms * 100
        if wake_recv_asyncio.p99_ms
        else 0.0
    )
    print(
        f"\nuvloop vs stock asyncio, p99: wake_send {send_delta:+.1f}%, "
        f"wake_recv {recv_delta:+.1f}% (positive = uvloop faster)"
    )

    partial_estimate_ms = sign_stats.p99_ms + wake_send_uvloop.p99_ms + wake_recv_uvloop.p99_ms
    print(
        f"\nPartial detect->fire p99 estimate (sign + wake_send + wake_recv, uvloop, "
        f"EXCLUDES network RTT / order construction / telemetry write): "
        f"{partial_estimate_ms:.3f}ms (informal only; target is <15ms end-to-end, not directly "
        f"comparable; see docs/GUIDE.md §7.2)"
    )

    _HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    is_new = not _HISTORY_CSV.exists()
    with _HISTORY_CSV.open("a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(_HISTORY_FIELDS)
        writer.writerow(
            [
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                _platform_tag(),
                f"{sign_stats.p50_ms:.4f}",
                f"{sign_stats.p99_ms:.4f}",
                f"{wake_send_asyncio.p50_ms:.4f}",
                f"{wake_send_asyncio.p99_ms:.4f}",
                f"{wake_recv_asyncio.p50_ms:.4f}",
                f"{wake_recv_asyncio.p99_ms:.4f}",
                f"{wake_send_uvloop.p50_ms:.4f}",
                f"{wake_send_uvloop.p99_ms:.4f}",
                f"{wake_recv_uvloop.p50_ms:.4f}",
                f"{wake_recv_uvloop.p99_ms:.4f}",
                f"{partial_estimate_ms:.4f}",
            ]
        )
    print(f"\nAppended results to {_HISTORY_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
