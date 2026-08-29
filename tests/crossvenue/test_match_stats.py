"""Tests for the narrowing counters `match_all` records.

The matcher's justification is a reduction factor, and a counter nobody asserts on is a number
that can quietly become wrong, which is this project's most-repeated failure mode. These pin
the arithmetic (`total_pairs` is the full cross, not the scored set), the empty-input case (stats
must describe the call that just happened rather than the previous one), and the invariant that
matters: candidates scored is a small fraction of pairs that exist.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kalshi_bot.crossvenue.fees import TakerFeeModel
from kalshi_bot.crossvenue.matching import MarketMatcher
from kalshi_bot.crossvenue.models import NormalizedMarket, SettlementTerms
from kalshi_bot.crossvenue.venues import Venue

_WHEN = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
_BOILERPLATE = "this market will resolve to yes if the outcome occurs per the rules below"


def _market(venue: Venue, market_id: str, subject: str) -> NormalizedMarket:
    """A market whose only discriminating word is `subject`."""
    question = f"will {subject} win the contest?"
    return NormalizedMarket(
        venue=venue,
        market_id=market_id,
        event_id=market_id,
        question=question,
        yes_label=subject,
        no_label=f"not {subject}",
        category="test",
        terms=SettlementTerms(close_time=_WHEN, expiry_time=_WHEN, scheduled_event_time=_WHEN),
        fee=TakerFeeModel(venue, 0.07, True, "test"),
        tick_dollars=0.01,
        min_order_contracts=1.0,
        claim_text=f"{question} {subject}",
        search_text=f"{question} {subject} {_BOILERPLATE}",
    )


def test_stats_is_none_before_the_first_match() -> None:
    assert MarketMatcher().stats is None


def test_total_pairs_is_the_full_cross_not_the_scored_set() -> None:
    """The headline denominator must be every pair that exists, or the reduction is overstated."""
    matcher = MarketMatcher()
    kalshi = [_market(Venue.KALSHI, f"k{i}", f"subject{i}") for i in range(20)]
    other = [_market(Venue.POLYMARKET_US, f"p{i}", f"subject{i}") for i in range(10)]

    matcher.match_all(kalshi, other)

    stats = matcher.stats
    assert stats is not None
    assert stats.kalshi_markets == 20
    assert stats.other_markets == 10
    assert stats.total_pairs == 200
    assert stats.candidates_scored < stats.total_pairs


def test_common_tokens_are_dropped_from_the_index() -> None:
    """Boilerplate shared by every market must not be indexed, since it narrows nothing."""
    matcher = MarketMatcher()
    kalshi = [_market(Venue.KALSHI, f"k{i}", f"subject{i}") for i in range(50)]
    other = [_market(Venue.POLYMARKET_US, f"p{i}", f"subject{i}") for i in range(50)]

    matcher.match_all(kalshi, other)

    stats = matcher.stats
    assert stats is not None
    assert stats.tokens_dropped_as_too_common > 0


def test_reduction_factor_reports_how_much_was_skipped() -> None:
    matcher = MarketMatcher()
    kalshi = [_market(Venue.KALSHI, f"k{i}", f"subject{i}") for i in range(40)]
    other = [_market(Venue.POLYMARKET_US, f"p{i}", f"subject{i}") for i in range(40)]

    matcher.match_all(kalshi, other)

    stats = matcher.stats
    assert stats is not None
    assert stats.reduction_factor > 1.0


def test_empty_side_records_stats_for_this_call_not_the_last_one() -> None:
    """A stale counter is worse than no counter: it reads as a measurement of the current call."""
    matcher = MarketMatcher()
    kalshi = [_market(Venue.KALSHI, f"k{i}", f"subject{i}") for i in range(5)]
    other = [_market(Venue.POLYMARKET_US, f"p{i}", f"subject{i}") for i in range(5)]
    matcher.match_all(kalshi, other)

    matcher.match_all(kalshi, [])

    stats = matcher.stats
    assert stats is not None
    assert stats.other_markets == 0
    assert stats.total_pairs == 0
    assert stats.candidates_scored == 0
    assert stats.reduction_factor == float("inf")
