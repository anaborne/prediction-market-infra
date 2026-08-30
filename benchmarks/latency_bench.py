"""Phase 9 latency benchmark suite.

Standalone, outside the pytest suite (same pattern as `scripts/live_*.py`). Run directly via
`uv run python benchmarks/latency_bench.py`, which also
appends the run's results to `benchmarks/history.csv`.

`--executor cpp:/path/to/executor_hotpath` swaps the executor process for the C++
reimplementation in `executor-hotpath-cpp` and leaves everything else alone: the same
`IPCPollerClient`, the same socket, the same frames, the same `latency_events` table. That is the
whole claim the substitution makes, so `poller_client.py` is neither modified nor subclassed here.
The `executor` column in `history.csv` says which side of the socket produced a row.

One asymmetry the comparison has to carry rather than hide. The Python executor dispatches every
accepted fire through `OrderDispatcher` and the fake REST client, and the C++ binary's dispatch
hook is empty, so it does less after the ack than the Python does. `wake_recv` closes before the
fire in both, so the span itself is unaffected, but the process is not equally loaded and
`wake_send` runs against an executor with less to do.

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

import argparse
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
_WAKE_ITERATIONS = 2000

# Discarded from the front of every sample, on both sides of every comparison. Nothing was
# discarded before 2026-08-30; the first iteration landed in the percentile set with a cold RSA
# context, a cold socket and a cold page cache, and at n=2000 the top twenty samples are what p99
# is. The C++ harness defaults to the same count, and a run of one with warm-up against a run of
# the other without it would be rigged in the direction the port wants.
_WARMUP_ITERATIONS = 200

# `IPCPollerClient` holds a bounded queue and `send_wake` drops onto a full one, by design: a wake
# channel that blocked its caller would block the fire path. A synchronous loop of 2200 sends fills
# the default 1000-deep queue and loses more than half of them, so the loop yields after each send
# and the writer keeps the depth at one or two.
#
# This also decides what `wake_send` measures. The span starts at `sent_at_ns`, before the enqueue,
# so under a burst it is mostly the wait behind everything already queued. Yielding makes it the
# write and the drain, which is the thing the executor is on the other end of.

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


# `executor` and `warmup` were added 2026-08-30 and the three rows that predate them are
# backfilled with `python` and `0`, both of which are what those runs did. The `p90` columns
# arrived at the same time and are empty in those rows, because the samples they would have been
# computed from are gone and a p90 interpolated from a p50 and a p99 would be a fabrication.
_HISTORY_FIELDS = [
    "timestamp_utc",
    "platform",
    "executor",
    "warmup",
    "sign_p50_ms",
    "sign_p90_ms",
    "sign_p99_ms",
    "wake_send_asyncio_p50_ms",
    "wake_send_asyncio_p90_ms",
    "wake_send_asyncio_p99_ms",
    "wake_recv_asyncio_p50_ms",
    "wake_recv_asyncio_p90_ms",
    "wake_recv_asyncio_p99_ms",
    "wake_send_uvloop_p50_ms",
    "wake_send_uvloop_p90_ms",
    "wake_send_uvloop_p99_ms",
    "wake_recv_uvloop_p50_ms",
    "wake_recv_uvloop_p90_ms",
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
    p90_ms: float
    p99_ms: float
    mean_ms: float
    n: int
    warmup: int

    @classmethod
    def from_samples(cls, samples: list[float], warmup: int) -> Stats:
        """Drop the leading `warmup` samples, then summarize the rest.

        `samples` has to arrive in the order it was measured, which is why the queries in
        `_read_stage_durations` order by `id` rather than taking whatever a table scan hands back.
        """
        kept = samples[warmup:]
        if not kept:
            raise ValueError(f"a warm-up of {warmup} consumed all {len(samples)} samples")
        return cls(
            p50_ms=_percentile(kept, 0.50),
            p90_ms=_percentile(kept, 0.90),
            p99_ms=_percentile(kept, 0.99),
            mean_ms=statistics.mean(kept),
            n=len(kept),
            warmup=warmup,
        )


def _report_line(label: str, stats: Stats, target_ms: float) -> str:
    verdict = "OK" if stats.p99_ms < target_ms else "OVER BUDGET"
    return (
        f"{label:<32} p50={stats.p50_ms:7.3f}ms  p90={stats.p90_ms:7.3f}ms  "
        f"p99={stats.p99_ms:7.3f}ms  n={stats.n:<5} warmup={stats.warmup:<5} "
        f"target=<{target_ms}ms  [{verdict}]"
    )


@dataclass(frozen=True)
class ExecutorSpec:
    """Which process serves the socket. `python` is in-process; `cpp:PATH` is spawned."""

    label: str
    binary: Path | None

    @classmethod
    def parse(cls, value: str) -> ExecutorSpec:
        if value == "python":
            return cls(label="python", binary=None)
        if value.startswith("cpp:"):
            binary = Path(value[len("cpp:") :]).expanduser()
            if not binary.is_file():
                raise argparse.ArgumentTypeError(f"no executor binary at {binary}")
            return cls(label="cpp", binary=binary)
        raise argparse.ArgumentTypeError(f"expected 'python' or 'cpp:PATH', got {value!r}")


def bench_sign(iterations: int = _SIGN_ITERATIONS, warmup: int = _WARMUP_ITERATIONS) -> Stats:
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
        for _ in range(warmup + iterations):
            timestamp = str(int(time.time() * 1000))
            start_ns = time.perf_counter_ns()
            signer.sign(timestamp, "GET", "/trade-api/v2/portfolio/orders")
            durations_ms.append((time.perf_counter_ns() - start_ns) / 1_000_000)

    return Stats.from_samples(durations_ms, warmup)


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


def _bench_wake(index: int) -> WakeMessage:
    """The wake both configurations send. Identical bytes on the wire either way."""
    return WakeMessage(
        schema_version=SCHEMA_VERSION,
        correlation_id=f"bench-{index}",
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


def _read_stage_durations(db_path: Path, stage: str) -> list[float]:
    """`duration_ms` for one stage, in the order the rows were written.

    `ORDER BY id` rather than whatever a scan returns, because the caller drops a warm-up prefix
    and `idx_latency_events_stage` makes the unordered result's order an implementation detail of
    the query planner.
    """
    conn = sqlite3.connect(db_path)
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT duration_ms FROM latency_events WHERE stage = ? ORDER BY id", (stage,)
            )
        ]
    finally:
        conn.close()


def _count_stage(db_path: Path, stage: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM latency_events WHERE stage = ?", (stage,)
        ).fetchone()
        return int(count)
    finally:
        conn.close()


async def _run_wake_roundtrip_python(
    total: int, telemetry_db_path: Path, socket_path: Path
) -> None:
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
        for i in range(total):
            client.send_wake(_bench_wake(i))
            await asyncio.sleep(0)
        await _wait_until(lambda: posted >= total, timeout_s=120.0)
    finally:
        await client.close()
        await server.close()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        poller_db.close()
        executor_db.close()


async def _run_wake_roundtrip_cpp(
    binary: Path, total: int, telemetry_db_path: Path, socket_path: Path
) -> None:
    """Same poller, same socket, same frames, a different process on the far end.

    The Python opens the telemetry file first so its own migrations decide the schema; the C++
    sink applies `CREATE TABLE IF NOT EXISTS` over whatever it finds and stamps `user_version`
    only on a file it created, so the order here is what keeps one side from surprising the other.

    Completion is the executor's own `wake_recv` row count and not the count of frames this
    process wrote. Terminating on frames-written leaves wakes sitting in the socket buffer that
    the executor is about to read, and drops them.
    """
    poller_db = TelemetryDB(telemetry_db_path)
    poller_db.initialize()

    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--socket",
        str(socket_path),
        "--telemetry-db",
        str(telemetry_db_path),
    )
    try:
        await _wait_until(socket_path.exists, timeout_s=10.0)
        client = IPCPollerClient(socket_path, poller_db)
        client.start()
        try:
            for i in range(total):
                client.send_wake(_bench_wake(i))
                await asyncio.sleep(0)
            await _wait_until(
                lambda: _count_stage(telemetry_db_path, "wake_recv") >= total, timeout_s=120.0
            )
        finally:
            await client.close()
    finally:
        # SIGTERM, then wait: `main.cpp` closes the telemetry sink after the serve loop returns,
        # so the drain that lands the last batch happens inside this wait.
        process.terminate()
        await process.wait()
        poller_db.close()


def bench_wake_roundtrip(
    run: Callable[..., Any],
    executor: ExecutorSpec,
    iterations: int = _WAKE_ITERATIONS,
    warmup: int = _WARMUP_ITERATIONS,
) -> tuple[Stats, Stats]:
    """Run the wake round trip under whichever loop runner `run` provides.

    `run` is `asyncio.run` or `uvloop.run`. Uses a `/tmp`-rooted socket dir, same as
    `tests/ipc/`, because `AF_UNIX` paths have a short OS-level length limit that a
    `pytest`-generated `tmp_path` can exceed.

    `run` still governs the poller's loop when the executor is the C++ binary. That is the point
    of keeping both rows: the loop under test is the one this process runs, and the far end is a
    separate process either way.
    """
    total = warmup + iterations
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        socket_path = Path(tmp) / "executor.sock"
        db_path = Path(tmp) / "telemetry.sqlite"
        if executor.binary is None:
            run(_run_wake_roundtrip_python(total, db_path, socket_path))
        else:
            run(_run_wake_roundtrip_cpp(executor.binary, total, db_path, socket_path))

        wake_send = _read_stage_durations(db_path, "wake_send")
        wake_recv = _read_stage_durations(db_path, "wake_recv")

    for stage, durations in (("wake_send", wake_send), ("wake_recv", wake_recv)):
        if len(durations) != total:
            raise RuntimeError(
                f"{stage}: {len(durations)} rows for {total} wakes; the run lost frames and the "
                f"percentiles would be computed over a sample nobody chose"
            )
    return Stats.from_samples(wake_send, warmup), Stats.from_samples(wake_recv, warmup)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 9 latency benchmark suite.")
    parser.add_argument(
        "--executor",
        type=ExecutorSpec.parse,
        default=ExecutorSpec(label="python", binary=None),
        help="python (default), or cpp:/path/to/executor_hotpath",
    )
    parser.add_argument("--sign-iterations", type=int, default=_SIGN_ITERATIONS)
    parser.add_argument("--wake-iterations", type=int, default=_WAKE_ITERATIONS)
    parser.add_argument(
        "--warmup",
        type=int,
        default=_WARMUP_ITERATIONS,
        help="iterations discarded from the front of every sample",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="print the results without appending a row to history.csv",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    executor = args.executor
    warmup = args.warmup

    print(f"Executor: {executor.label}" + (f" ({executor.binary})" if executor.binary else ""))
    print(f"Signature computation: {args.sign_iterations} iterations, RSA-2048 PSS...")
    sign_stats = bench_sign(args.sign_iterations, warmup)
    print(_report_line("sign()", sign_stats, target_ms=3.0))

    print(f"\nPoller->executor wake round trip: {args.wake_iterations} messages, stock asyncio...")
    wake_send_asyncio, wake_recv_asyncio = bench_wake_roundtrip(
        asyncio.run, executor, args.wake_iterations, warmup
    )
    print(_report_line("wake_send (asyncio)", wake_send_asyncio, target_ms=1.0))
    print(_report_line("wake_recv (asyncio)", wake_recv_asyncio, target_ms=1.0))

    print(f"\nPoller->executor wake round trip: {args.wake_iterations} messages, uvloop...")
    wake_send_uvloop, wake_recv_uvloop = bench_wake_roundtrip(
        uvloop.run, executor, args.wake_iterations, warmup
    )
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
    if executor.label != "python":
        # The signer is not on either executor's fire path; it lives in `rest_client.py`, past
        # `dispatch()`. This row's sign number is this interpreter's, on both kinds of row.
        print(
            "\nsign() above is the Python signer in every row. Signing is not in the executor "
            "process on either side, so swapping the executor does not move it."
        )

    if args.no_history:
        return 0

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
                executor.label,
                warmup,
                f"{sign_stats.p50_ms:.4f}",
                f"{sign_stats.p90_ms:.4f}",
                f"{sign_stats.p99_ms:.4f}",
                f"{wake_send_asyncio.p50_ms:.4f}",
                f"{wake_send_asyncio.p90_ms:.4f}",
                f"{wake_send_asyncio.p99_ms:.4f}",
                f"{wake_recv_asyncio.p50_ms:.4f}",
                f"{wake_recv_asyncio.p90_ms:.4f}",
                f"{wake_recv_asyncio.p99_ms:.4f}",
                f"{wake_send_uvloop.p50_ms:.4f}",
                f"{wake_send_uvloop.p90_ms:.4f}",
                f"{wake_send_uvloop.p99_ms:.4f}",
                f"{wake_recv_uvloop.p50_ms:.4f}",
                f"{wake_recv_uvloop.p90_ms:.4f}",
                f"{wake_recv_uvloop.p99_ms:.4f}",
                f"{partial_estimate_ms:.4f}",
            ]
        )
    print(f"\nAppended results to {_HISTORY_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
