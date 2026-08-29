"""Tests for `execution.position_sizing`.

`size_position()` is pure arithmetic, with no clocks, no I/O, and no fakes needed. Hand-computed
expected values exercise the Kelly formula itself; the remaining tests exercise each guard
(fractional scaling, the balance-pct ceiling, the count floor, degenerate inputs) in isolation.
"""

from __future__ import annotations

import pytest

from kalshi_bot.execution.position_sizing import BalanceCache, size_position

_GENEROUS_CEILING = 1.0  # effectively "no ceiling" for tests isolating the Kelly formula itself


def test_full_kelly_matches_the_textbook_formula() -> None:
    """edge=0.20 at price=0.50: f* = edge / (1 - price) = 0.20 / 0.50 = 0.40 (40% of bankroll)."""
    count = size_position(
        edge=0.20,
        kalshi_price=0.50,
        balance_dollars=1000.0,
        kelly_fraction=1.0,  # full Kelly, no scaling
        max_position_pct_of_balance=_GENEROUS_CEILING,
        min_contract_count=1,
    )
    # 40% of $1000 = $400 staked at $0.50/contract = 800 contracts.
    assert count == 800


def test_kelly_fraction_scales_the_stake_down() -> None:
    """The same edge at a 15% fractional Kelly stakes 15% of what full Kelly would."""
    full = size_position(
        edge=0.20,
        kalshi_price=0.50,
        balance_dollars=1000.0,
        kelly_fraction=1.0,
        max_position_pct_of_balance=_GENEROUS_CEILING,
        min_contract_count=1,
    )
    fractional = size_position(
        edge=0.20,
        kalshi_price=0.50,
        balance_dollars=1000.0,
        kelly_fraction=0.15,
        max_position_pct_of_balance=_GENEROUS_CEILING,
        min_contract_count=1,
    )
    assert fractional == pytest.approx(full * 0.15, abs=1)  # integer rounding


def test_a_bigger_edge_stakes_more_than_a_smaller_one() -> None:
    small_edge = size_position(
        edge=0.02,
        kalshi_price=0.50,
        balance_dollars=1000.0,
        kelly_fraction=0.15,
        max_position_pct_of_balance=_GENEROUS_CEILING,
        min_contract_count=1,
    )
    big_edge = size_position(
        edge=0.20,
        kalshi_price=0.50,
        balance_dollars=1000.0,
        kelly_fraction=0.15,
        max_position_pct_of_balance=_GENEROUS_CEILING,
        min_contract_count=1,
    )
    assert big_edge > small_edge


def test_max_position_pct_of_balance_caps_a_large_kelly_stake() -> None:
    """A big enough edge would stake far more than a 2%-of-balance ceiling allows."""
    count = size_position(
        edge=0.90,  # deliberately huge, to force the ceiling to be the binding constraint
        kalshi_price=0.10,
        balance_dollars=1000.0,
        kelly_fraction=1.0,
        max_position_pct_of_balance=0.02,
        min_contract_count=1,
    )
    # 2% of $1000 = $20, at $0.10/contract = 200 contracts, regardless of how large edge is.
    assert count == 200


def test_min_contract_count_floors_a_tiny_stake() -> None:
    """A marginal edge or a small balance can round to fewer contracts than the floor allows."""
    count = size_position(
        edge=0.001,
        kalshi_price=0.50,
        balance_dollars=10.0,
        kelly_fraction=0.15,
        max_position_pct_of_balance=0.02,
        min_contract_count=5,
    )
    assert count == 5


def test_zero_balance_falls_back_to_the_floor() -> None:
    """No balance data yet (a fresh executor start, before the first poll) must not divide by
    zero or size a real order off of nothing. It degrades to the same floor a fixed-count
    deployment would have used."""
    count = size_position(
        edge=0.20,
        kalshi_price=0.50,
        balance_dollars=0.0,
        kelly_fraction=0.15,
        max_position_pct_of_balance=0.02,
        min_contract_count=3,
    )
    assert count == 3


@pytest.mark.parametrize("price", [0.0, 1.0, -0.1, 1.1])
def test_untradeable_price_falls_back_to_the_floor(price: float) -> None:
    """A price outside (0, 1) is garbage on the wire and no real quote, so it takes the same
    fallback as an unknown balance, never a crash from dividing by (1 - price) == 0."""
    count = size_position(
        edge=0.20,
        kalshi_price=price,
        balance_dollars=1000.0,
        kelly_fraction=0.15,
        max_position_pct_of_balance=0.02,
        min_contract_count=2,
    )
    assert count == 2


def test_negative_edge_never_shorts_the_floor() -> None:
    """The EV gate requires edge > 0 to fire at all, so this should never happen in practice --
    but a negative edge must still floor at min_contract_count, not go negative or size zero."""
    count = size_position(
        edge=-0.05,
        kalshi_price=0.50,
        balance_dollars=1000.0,
        kelly_fraction=0.15,
        max_position_pct_of_balance=0.02,
        min_contract_count=4,
    )
    assert count == 4


def test_negative_kelly_fraction_config_does_not_invert_the_stake() -> None:
    count = size_position(
        edge=0.20,
        kalshi_price=0.50,
        balance_dollars=1000.0,
        kelly_fraction=-1.0,
        max_position_pct_of_balance=0.02,
        min_contract_count=1,
    )
    assert count == 1  # clamped to the floor, not a negative or inverted stake


def test_balance_cache_defaults_to_zero_and_is_mutated_in_place() -> None:
    cache = BalanceCache()
    assert cache.balance_dollars == 0.0

    cache.balance_dollars = 500.0
    cache.updated_at_ms = 1_000

    assert cache.balance_dollars == 500.0
    assert cache.updated_at_ms == 1_000
