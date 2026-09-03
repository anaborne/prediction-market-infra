"""The matcher benchmark's counts, pinned at a corpus size the test suite can afford.

The README's benchmark block claims every count it prints is the same on any machine and in any
process. Until this file existed that claim was prose with no mechanism under it, and prose
decays silently (`ENGINEERING.md`). The counts also ride on `math.log` in `crossvenue/text.py`,
which is libm, so a platform whose logarithm differs in the last bits could reorder IDF ties and
move a market from one verdict to another without anything failing.

What is pinned here is the generator in `benchmarks/synthetic_corpus.py` and the matcher, at
800 x 300 markets with 40 planted pairs, which runs in a fraction of a second. The 20,000 x 6,000
counts quoted in the README are not pinned, because that run takes about fifteen seconds. A
change in the scorer, the index, or the generator moves the small counts and the large ones
together, so this is the tripwire for the block.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kalshi_bot.crossvenue.matching import MarketMatcher
from kalshi_bot.crossvenue.models import MatchConfidence, MatchedPair

_BENCHMARKS_DIR = Path(__file__).resolve().parents[2] / "benchmarks"
if str(_BENCHMARKS_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS_DIR))

from synthetic_corpus import Corpus, build_corpus  # noqa: E402

_KALSHI_COUNT = 800
_OTHER_COUNT = 300
_PLANTED = 40
_SEED = 20260825

# Measured from this repository at the size above. Every one of them is a property of the
# generator and the matcher, and of no exchange.
_EXPECTED_VERDICTS = {
    MatchConfidence.IDENTICAL: 40,
    MatchConfidence.STRONG: 0,
    MatchConfidence.WEAK: 9,
    MatchConfidence.REJECTED: 5,
}
_EXPECTED_CLEARED_THE_GATE = 54
_EXPECTED_TOTAL_PAIRS = 240_000
_EXPECTED_INDEXED_TOKENS = 166
_EXPECTED_TOKENS_DROPPED = 25
_EXPECTED_CANDIDATES_SCORED = 3_003


def _run() -> tuple[Corpus, MarketMatcher, list[MatchedPair]]:
    """Build the corpus and match it, the way `benchmarks/matcher_bench.py` does."""
    corpus = build_corpus(_KALSHI_COUNT, _OTHER_COUNT, planted_pairs=_PLANTED, seed=_SEED)
    matcher = MarketMatcher()
    pairs = matcher.match_all(corpus.kalshi, corpus.other)
    return corpus, matcher, pairs


@pytest.fixture(scope="module")
def matched() -> tuple[Corpus, MarketMatcher, list[MatchedPair]]:
    return _run()


def test_verdict_counts_are_the_same_on_every_run(
    matched: tuple[Corpus, MarketMatcher, list[MatchedPair]],
) -> None:
    """The verdict split is the count most likely to move under a scoring change."""
    _, _, pairs = matched

    verdicts = {
        confidence: sum(1 for pair in pairs if pair.confidence is confidence)
        for confidence in MatchConfidence
    }

    assert verdicts == _EXPECTED_VERDICTS
    assert len(pairs) == _EXPECTED_CLEARED_THE_GATE


def test_narrowing_counters_are_the_same_on_every_run(
    matched: tuple[Corpus, MarketMatcher, list[MatchedPair]],
) -> None:
    """The reduction factor is the benchmark's headline, so its two inputs are pinned."""
    _, matcher, _ = matched

    stats = matcher.stats
    assert stats is not None
    assert stats.kalshi_markets == _KALSHI_COUNT
    assert stats.other_markets == _OTHER_COUNT
    assert stats.total_pairs == _EXPECTED_TOTAL_PAIRS
    assert stats.indexed_tokens == _EXPECTED_INDEXED_TOKENS
    assert stats.tokens_dropped_as_too_common == _EXPECTED_TOKENS_DROPPED
    assert stats.candidates_scored == _EXPECTED_CANDIDATES_SCORED


def test_every_planted_pair_is_recovered_and_nothing_else_is_identical(
    matched: tuple[Corpus, MarketMatcher, list[MatchedPair]],
) -> None:
    """Filler markets draw from disjoint per-venue vocabularies, so an unplanted `IDENTICAL`
    is a defect and never a coincidence of the generator."""
    corpus, _, pairs = matched

    identical = {
        (pair.kalshi.market_id, pair.other.market_id)
        for pair in pairs
        if pair.confidence is MatchConfidence.IDENTICAL
    }

    assert identical == corpus.planted_keys
    assert len(identical) == _PLANTED


def test_a_second_build_and_match_in_the_same_process_agrees() -> None:
    """A count that changes on the second pass is a count carrying state from the first.

    This covers repetition inside one process. Independence from `PYTHONHASHSEED`, which is what
    makes the counts the same across processes, is checked by running the suite under several
    seeds.
    """
    corpus, matcher, pairs = _run()

    verdicts = {
        confidence: sum(1 for pair in pairs if pair.confidence is confidence)
        for confidence in MatchConfidence
    }
    stats = matcher.stats
    assert stats is not None

    assert verdicts == _EXPECTED_VERDICTS
    assert stats.candidates_scored == _EXPECTED_CANDIDATES_SCORED
    assert corpus.planted_pairs == _PLANTED
