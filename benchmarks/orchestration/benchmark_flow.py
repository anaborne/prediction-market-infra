"""Prefect flow that orchestrates the repo's benchmark pipeline: generate a synthetic
corpus, run the matcher benchmark against it, run the latency benchmark, and log both
results with retries and structured logging instead of the ad-hoc sequence of shell
steps CI previously ran.

Why this exists as a flow instead of the two CI steps it used to be:
the two benchmarks are genuinely dependent work (matcher_bench needs the corpus
generator; both need a clean environment) with different failure modes (a flaky
network blip during dependency resolution vs. a real regression), and until now
nothing retried a transient failure or recorded *which* stage failed without reading
raw CI logs. This flow makes that dependency graph and retry policy explicit and
runnable on a schedule as well as on every push.

Run it directly:
    uv run python benchmarks/orchestration/benchmark_flow.py

Or deploy it on a schedule (defined at the bottom of this file, not started by default,
see the __main__ block):
    uv run python benchmarks/orchestration/benchmark_flow.py --serve
"""

from __future__ import annotations

import csv
import re
import subprocess
import sys
import time
from pathlib import Path

from prefect import flow, get_run_logger, task

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"
MATCHER_HISTORY_CSV = BENCHMARKS_DIR / "matcher_history.csv"

_MATCHER_FIELDS = [
    "platform",
    "kalshi_markets",
    "other_markets",
    "reduction_factor",
    "candidates_scored",
    "wall_time_s",
    "planted_recovered",
    "planted_total",
]


@task(
    name="latency-benchmark",
    retries=1,
    retry_delay_seconds=10,
    timeout_seconds=300,
)
def run_latency_benchmark() -> str:
    """Runs the existing latency_bench.py unmodified. It already appends its own row
    to benchmarks/history.csv (see that script's own `main()`); this task's job is
    orchestration: retry on a transient failure, surface stdout, propagate a real
    failure. It re-measures nothing."""
    logger = get_run_logger()
    result = subprocess.run(
        [sys.executable, "latency_bench.py"],
        cwd=BENCHMARKS_DIR,
        capture_output=True,
        text=True,
        timeout=280,
    )
    logger.info(result.stdout)
    if result.returncode != 0:
        logger.error(result.stderr)
        raise RuntimeError(f"latency_bench.py exited {result.returncode}")
    return result.stdout


@task(
    name="matcher-benchmark",
    retries=1,
    retry_delay_seconds=10,
    timeout_seconds=300,
)
def run_matcher_benchmark(kalshi: int = 4000, other: int = 1500, planted: int = 100) -> str:
    """Runs the existing matcher_bench.py unmodified, with the same small corpus size
    CI uses (this is a benchmark *smoke test* and no full-size production run, see
    that script's own docstring for why the numbers are corpus-size-dependent)."""
    logger = get_run_logger()
    result = subprocess.run(
        [
            sys.executable,
            "matcher_bench.py",
            "--kalshi",
            str(kalshi),
            "--other",
            str(other),
            "--planted",
            str(planted),
        ],
        cwd=BENCHMARKS_DIR,
        capture_output=True,
        text=True,
        timeout=280,
    )
    logger.info(result.stdout)
    if result.returncode != 0:
        logger.error(result.stderr)
        raise RuntimeError(f"matcher_bench.py exited {result.returncode}")
    return result.stdout


@task(name="log-matcher-result")
def log_matcher_result(stdout: str) -> dict[str, str]:
    """matcher_bench.py doesn't persist its own results (unlike latency_bench.py,
    which self-logs). This task closes that gap: parse the same structured stdout the
    script already prints and append one row per run to matcher_history.csv, mirroring
    latency_bench.py's own pattern so both benchmarks have a run-over-run history."""
    logger = get_run_logger()

    def _num(pattern: str, text: str) -> str:
        m = re.search(pattern, text)
        if not m:
            raise ValueError(f"Could not parse {pattern!r} out of matcher_bench output")
        return m.group(1).replace(",", "")

    row = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": _num(r"platform:\s*(\S+)", stdout),
        "kalshi_markets": _num(r"Kalshi-side markets\s+([\d,]+)", stdout),
        "other_markets": _num(r"counterparty markets\s+([\d,]+)", stdout),
        "reduction_factor": _num(r"reduction factor\s+([\d,]+)x", stdout),
        "candidates_scored": _num(r"candidates scored\s+([\d,]+)", stdout),
        "wall_time_s": _num(r"wall time \(median of \d+\)\s+([\d.]+)s", stdout),
        "planted_recovered": _num(r"planted pairs recovered\s+([\d,]+)", stdout),
        "planted_total": _num(r"planted pairs\s+([\d,]+)", stdout),
    }

    is_new = not MATCHER_HISTORY_CSV.exists()
    with MATCHER_HISTORY_CSV.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp_utc", *_MATCHER_FIELDS])
        if is_new:
            writer.writeheader()
        writer.writerow(row)
    logger.info(f"Appended matcher benchmark result to {MATCHER_HISTORY_CSV.name}: {row}")
    return row


@flow(name="benchmark-pipeline", log_prints=True)
def benchmark_pipeline_flow(kalshi: int = 4000, other: int = 1500, planted: int = 100) -> None:
    """The DAG: latency and matcher benchmarks run independently (they share no state), then the
    matcher result is parsed and logged. Prefect tracks this dependency graph, retries each task
    independently on transient failure, and gives every run a timestamped, inspectable history, the
    actual value over the prior "two shell steps in a CI YAML file" setup, which had no retry and no
    persisted matcher history.
    """
    latency_stdout = run_latency_benchmark()
    matcher_stdout = run_matcher_benchmark(kalshi=kalshi, other=other, planted=planted)
    log_matcher_result(matcher_stdout)
    print("Benchmark pipeline complete.")
    print(latency_stdout.splitlines()[0] if latency_stdout else "(no latency output)")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        benchmark_pipeline_flow.serve(name="nightly-benchmark-smoke", cron="0 7 * * *")
    else:
        benchmark_pipeline_flow()
