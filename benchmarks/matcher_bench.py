"""Benchmark for the cross-venue matcher's narrowing, on a generated corpus.

Standalone, outside the pytest suite (same pattern as `latency_bench.py`). Run directly:

    uv run python benchmarks/matcher_bench.py
    uv run python benchmarks/matcher_bench.py --kalshi 40000 --other 12000

What this reproduces, and what it does not. It reproduces the *mechanism*: that an
IDF-weighted inverted index over market wording turns a quadratic cross into a linear-ish scored
set, and that the settlement gate then refuses most of what the similarity score was willing to
pass. It does not reproduce the production figures, which were measured on a real
whole-venue scan of two live exchanges; those markets are not in this repository and this script
cannot produce them. The numbers printed below are properties of `synthetic_corpus.py`'s
generator at the requested size, and the script says so on every run rather than in a footnote,
because a benchmark that prints a number resembling a production claim will be read as that
claim.

The corpus plants a known number of true pairs, worded as each venue would word them. That plant
is what makes a low match count readable: a matcher that finds none of the planted pairs is
broken, and without the plant it would be indistinguishable from a matcher that is merely strict.
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
import time

from synthetic_corpus import build_corpus

from kalshi_bot.crossvenue.matching import MarketMatcher
from kalshi_bot.crossvenue.models import MatchConfidence

_DEFAULT_KALSHI = 20_000
_DEFAULT_OTHER = 6_000
_DEFAULT_PLANTED = 250


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Read the corpus size off the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kalshi", type=int, default=_DEFAULT_KALSHI)
    parser.add_argument("--other", type=int, default=_DEFAULT_OTHER)
    parser.add_argument("--planted", type=int, default=_DEFAULT_PLANTED)
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Run the match this many times and report the median wall time.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Generate a corpus, match it, and print what the narrowing actually achieved."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    build_started = time.perf_counter()
    corpus = build_corpus(args.kalshi, args.other, planted_pairs=args.planted, seed=args.seed)
    build_seconds = time.perf_counter() - build_started

    matcher = MarketMatcher()
    durations: list[float] = []
    pairs = []
    for _ in range(max(1, args.repeats)):
        started = time.perf_counter()
        pairs = matcher.match_all(corpus.kalshi, corpus.other)
        durations.append(time.perf_counter() - started)
    stats = matcher.stats
    assert stats is not None  # noqa: S101 - match_all always records before returning

    by_confidence = {
        confidence: sum(1 for pair in pairs if pair.confidence is confidence)
        for confidence in MatchConfidence
    }
    elapsed = statistics.median(durations)

    print(f"platform: {platform.system()}-{platform.machine()}-py{platform.python_version()}")
    print(f"corpus:   synthetic, seed={args.seed}, generated in {build_seconds:.2f}s")
    print()
    print(f"  Kalshi-side markets        {stats.kalshi_markets:>12,}")
    print(f"  counterparty markets       {stats.other_markets:>12,}")
    print(f"  full cross (pairs)         {stats.total_pairs:>12,}")
    print(f"  tokens indexed             {stats.indexed_tokens:>12,}")
    print(f"  tokens dropped (too common){stats.tokens_dropped_as_too_common:>12,}")
    print(f"  candidates scored          {stats.candidates_scored:>12,}")
    print(f"  reduction factor           {stats.reduction_factor:>12,.0f}x")
    print(f"  wall time (median of {max(1, args.repeats)})   {elapsed:>12.2f}s")
    print()
    print(f"  cleared the score gate     {len(pairs):>12,}")
    print()
    print("  Verdicts on the candidates that cleared it:")
    for confidence, count in by_confidence.items():
        print(f"    {confidence.value:<12} {count:>10,}")
    print()

    identical = [pair for pair in pairs if pair.confidence is MatchConfidence.IDENTICAL]
    recovered = {
        (pair.kalshi.market_id, pair.other.market_id) for pair in identical
    } & corpus.planted_keys
    unplanted = len(identical) - len(recovered)
    print(f"  planted pairs              {corpus.planted_pairs:>12,}")
    print(f"  planted pairs recovered    {len(recovered):>12,}")
    print(f"  identical, not planted     {unplanted:>12,}  (expected 0)")
    print()
    print(
        "  These are properties of the generated corpus at this size, not of any real exchange.\n"
        "  The production scan they mirror in shape is not reproducible from this repository:\n"
        "  it read two live venues, and those listings are not vendored here."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
