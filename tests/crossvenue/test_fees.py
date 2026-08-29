"""Tests for the per-venue fee models.

Both venues' parameters are read off the wire, so these tests are written against payload shapes
captured from live responses (2026-08-22, maker fields 2026-08-24) rather than against the
published schema. The Kalshi maker expectations are the fee schedule PDF's formula
(`M x 0.0175 x C x P(1-P)`, round up) with the maker multiplier implied by `fee_type`
(`docs/GUIDE.md` §2.7).
"""

from __future__ import annotations

import math

import pytest

from kalshi_bot.crossvenue.fees import (
    UnsupportedFeeModelError,
    kalshi_fee_model,
    polymarket_fee_model,
)
from kalshi_bot.crossvenue.venues import Venue

_GAMMA_SPORTS = {
    "feesEnabled": True,
    "feeType": "sports_fees_v3",
    "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15},
}


def test_kalshi_fee_matches_the_published_quadratic() -> None:
    model = kalshi_fee_model({"ticker": "KXBTCD", "fee_type": "quadratic", "fee_multiplier": 1})
    assert model.venue is Venue.KALSHI
    # Peak fee is at 50c: ceil(0.07 * 1 * 0.25 * 100)/100 = $0.0175 -> rounded up to $0.02.
    assert model.fee_dollars(0.5, 1) == pytest.approx(0.02)
    # On a batch the rounding applies once, not per contract.
    assert model.fee_dollars(0.5, 100) == pytest.approx(math.ceil(0.07 * 100 * 0.25 * 100) / 100)


def test_kalshi_zero_multiplier_is_fee_free() -> None:
    model = kalshi_fee_model({"ticker": "X", "fee_type": "quadratic", "fee_multiplier": 0})
    assert model.fee_dollars(0.5, 1000) == 0.0


def test_kalshi_default_fee_type_is_free_maker() -> None:
    model = kalshi_fee_model({"ticker": "KXBTCD", "fee_type": "quadratic", "fee_multiplier": 1})
    assert model.maker_rate == 0.0
    assert model.maker_fee_dollars(0.5, 10_000) == 0.0


def test_kalshi_maker_fee_type_prices_both_sides() -> None:
    """`quadratic_with_maker_fees`: taker identical to plain quadratic, maker at 0.0175."""
    model = kalshi_fee_model(
        {"ticker": "KXNFLGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
    )
    plain = kalshi_fee_model({"ticker": "X", "fee_type": "quadratic", "fee_multiplier": 1})
    assert model.fee_dollars(0.5, 100) == plain.fee_dollars(0.5, 100)
    # Maker at mid on a 100-lot: ceil(0.0175 * 100 * 0.25 * 100)/100 = $0.44.
    assert model.maker_rate == pytest.approx(0.0175)
    assert model.maker_fee_dollars(0.5, 100) == pytest.approx(0.44)
    # And the schedule's round-up applies to the maker charge too: one contract at mid is
    # 0.4375c of raw fee, charged as a whole cent.
    assert model.maker_fee_dollars(0.5, 1) == pytest.approx(0.01)


def test_kalshi_combo_maker_fee_type_doubles_the_maker_rate() -> None:
    model = kalshi_fee_model(
        {"ticker": "X", "fee_type": "quadratic_with_combo_maker_fees", "fee_multiplier": 1}
    )
    assert model.maker_rate == pytest.approx(0.035)


def test_kalshi_mlb_series_base_halves_the_taker_but_not_the_maker() -> None:
    """`KXMLBGAME` live: taker multiplier 0.5 on the series, maker multiplier 1 from fee_type.

    The API's `fee_multiplier` scales the taker only; the maker multiplier comes from
    `fee_type` and nowhere else on the wire.
    """
    model = kalshi_fee_model(
        {"ticker": "KXMLBGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5}
    )
    assert model.rate == pytest.approx(0.035)
    assert model.maker_rate == pytest.approx(0.0175)


def test_kalshi_refuses_an_unimplemented_fee_type() -> None:
    """`flat` exists in the schema (zero live series) and is a different curve, not a rate."""
    with pytest.raises(UnsupportedFeeModelError):
        kalshi_fee_model({"ticker": "X", "fee_type": "flat", "fee_multiplier": 1})


def test_kalshi_refuses_an_unreadable_multiplier() -> None:
    with pytest.raises(UnsupportedFeeModelError):
        kalshi_fee_model({"ticker": "X", "fee_type": "quadratic", "fee_multiplier": "n/a"})


def test_polymarket_fee_reads_the_rate_off_the_market() -> None:
    model = polymarket_fee_model(_GAMMA_SPORTS)
    assert model.rate == pytest.approx(0.05)
    assert model.fee_dollars(0.5, 100) == pytest.approx(0.05 * 100 * 0.25)


def test_polymarket_fee_free_market_has_no_schedule() -> None:
    model = polymarket_fee_model({"feesEnabled": False, "feeType": None})
    assert model.rate == 0.0
    assert model.fee_dollars(0.5, 10_000) == 0.0


def test_polymarket_refuses_an_exponent_it_does_not_implement() -> None:
    market = dict(_GAMMA_SPORTS, feeSchedule={"exponent": 2, "rate": 0.05, "takerOnly": True})
    with pytest.raises(UnsupportedFeeModelError):
        polymarket_fee_model(market)


def test_fee_basis_accumulation_is_exact_where_a_vwap_is_not() -> None:
    """A multi-level fill's fee must come from the summed basis, not the average price.

    `p(1 - p)` is concave, so averaging the price first understates the fee, by more the wider
    the fill spans. Both venues charge per fill, so both are priced from the accumulated basis.
    """
    model = polymarket_fee_model(_GAMMA_SPORTS)
    # 100 contracts at 10c and 100 at 90c: the same basis, an average price of 50c.
    basis = 100 * 0.1 * 0.9 + 100 * 0.9 * 0.1
    exact = model.fee_dollars_from_basis(basis)
    naive = model.fee_dollars(0.5, 200)
    assert exact < naive
    assert exact == pytest.approx(0.05 * basis)
