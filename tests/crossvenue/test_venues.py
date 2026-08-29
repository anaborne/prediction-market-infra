"""Tests for the US-execution policy.

These encode a legal constraint, so they are written to fail loudly if the policy table is
edited: a change to who may trade where should be a deliberate act with an ADR behind it, not a
side effect.
"""

from __future__ import annotations

import pytest

from kalshi_bot.crossvenue.venues import (
    DataAccess,
    Venue,
    VenueExecutionError,
    assert_us_executable,
    pair_is_us_executable,
    policy_for,
    us_executable,
)


def test_kalshi_is_executable_by_a_us_person() -> None:
    assert us_executable(Venue.KALSHI) is True
    assert policy_for(Venue.KALSHI).data_access is DataAccess.PUBLIC


def test_international_polymarket_is_observation_only() -> None:
    """Its book is the deepest public data available and is still not a leg a US person can fill."""
    assert us_executable(Venue.POLYMARKET_INTL) is False
    assert policy_for(Venue.POLYMARKET_INTL).data_access is DataAccess.PUBLIC


def test_polymarket_us_is_executable_and_its_book_is_public() -> None:
    """The venue a US person trades also publishes a public book, on a different host.

    An earlier version of this policy said the opposite, having probed only the authenticated
    host (`api.polymarket.us`, 401 everywhere) and missed `gateway.polymarket.us`, which serves
    markets, books, and BBO with no credentials at all. Trading and streaming still need a key;
    `data_access` describes reading the book.
    """
    assert us_executable(Venue.POLYMARKET_US) is True
    assert policy_for(Venue.POLYMARKET_US).data_access is DataAccess.PUBLIC


def test_the_kalshi_polymarket_intl_pair_is_not_executable() -> None:
    assert pair_is_us_executable(Venue.KALSHI, Venue.POLYMARKET_INTL) is False
    assert pair_is_us_executable(Venue.KALSHI, Venue.POLYMARKET_US) is True


def test_assert_us_executable_refuses_with_its_reasoning() -> None:
    with pytest.raises(VenueExecutionError, match="not registered for US persons"):
        assert_us_executable(Venue.POLYMARKET_INTL)
    assert_us_executable(Venue.KALSHI)


def test_an_unknown_venue_does_not_default_to_permitted() -> None:
    with pytest.raises(KeyError):
        policy_for("some_new_venue")  # type: ignore[arg-type]
