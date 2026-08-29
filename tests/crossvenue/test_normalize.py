"""Tests for lowering each venue's raw payload into `NormalizedMarket`.

This module owns every wire-field read in the package, so the fixtures here are trimmed copies of
responses captured live on 2026-08-22, including the fields that diverge from the published
schema. A fixture shaped like what the author *believed* the API returns is exactly how
`ingest/strike_ladder.py` passed 169 tests while raising `KeyError` on every live call.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kalshi_bot.crossvenue.fees import TakerFeeModel
from kalshi_bot.crossvenue.models import Comparator
from kalshi_bot.crossvenue.normalize import (
    decode_json_array,
    normalize_kalshi_market,
    normalize_polymarket_market,
)
from kalshi_bot.crossvenue.venues import Venue

_FEE = TakerFeeModel(Venue.KALSHI, 0.07, True, "test")

# Captured from `GET /markets/KXBTCD-26AUG2317-T86749.99`. Note `floor_strike` with
# `strike_type: "greater"`, ISO-8601 times with no `*_ts` counterparts, and `price_ranges`.
_KALSHI_CRYPTO = {
    "ticker": "KXBTCD-26AUG2317-T86749.99",
    "event_ticker": "KXBTCD-26AUG2317",
    "title": "Bitcoin price on Aug 23, 2026?",
    "yes_sub_title": "$86,750 or above",
    "no_sub_title": "$86,750 or above",
    "strike_type": "greater",
    "floor_strike": 86749.99,
    "close_time": "2026-08-23T21:00:00Z",
    "expected_expiration_time": "2026-08-23T21:05:00Z",
    "expiration_time": "2026-08-30T21:00:00Z",
    "can_close_early": True,
    "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    "rules_primary": (
        "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index "
        "(BRTI) before 5 PM EDT is above 86749.99 at 5 PM EDT on Aug 23, 2026, then the market "
        "resolves to Yes."
    ),
}

_KALSHI_EVENT = {
    "event_ticker": "KXBTCD-26AUG2317",
    "series_ticker": "KXBTCD",
    "title": "Bitcoin price on Aug 23, 2026?",
    "category": "Financials",
    "settlement_sources": [{"name": "CF Benchmarks", "url": "https://www.cfbenchmarks.com/"}],
}

# Captured from Gamma. `outcomes` and `clobTokenIds` are JSON arrays *inside JSON strings*.
_GAMMA_MONEYLINE = {
    "conditionId": "0x0235a1bf",
    "question": "Tampa Bay Rays vs. Baltimore Orioles",
    "description": "In the upcoming MLB game between the Tampa Bay Rays and Baltimore Orioles",
    "outcomes": '["Tampa Bay Rays", "Baltimore Orioles"]',
    "clobTokenIds": '["8088", "6120"]',
    "endDate": "2026-08-29T23:05:00Z",
    "gameStartTime": "2026-08-22 23:05:00+00",
    "resolutionSource": "https://www.mlb.com/",
    "orderPriceMinTickSize": 0.01,
    "orderMinSize": 5,
    "sportsMarketType": "moneyline",
    "feesEnabled": True,
    "feeType": "sports_fees_v3",
    "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15},
    "events": [{"slug": "mlb-tb-bal-2026-08-22", "title": "Tampa Bay Rays vs. Baltimore Orioles"}],
}


def test_decode_json_array_handles_json_inside_json() -> None:
    assert decode_json_array('["Yes", "No"]') == ["Yes", "No"]
    assert decode_json_array(["Yes", "No"]) == ["Yes", "No"]
    assert decode_json_array("not json") == []
    assert decode_json_array(None) == []


def test_kalshi_greater_strike_is_strict_against_floor_strike() -> None:
    """`strike_type: "greater"` with `floor_strike: 86749.99` is quoted "$86,750 or above"."""
    market = normalize_kalshi_market(_KALSHI_CRYPTO, _KALSHI_EVENT, _FEE)
    assert market.terms.comparator is Comparator.GREATER
    assert market.terms.threshold == pytest.approx(86_749.99)
    assert market.terms.threshold_units == "usd"
    assert market.category == "financials"


def test_kalshi_less_strike_reads_cap_strike() -> None:
    """Verified live on `KXHIGHNY`: `cap_strike: 80` with `less` is quoted "79° or below"."""
    market = normalize_kalshi_market(
        {**_KALSHI_CRYPTO, "strike_type": "less", "floor_strike": None, "cap_strike": 80},
        _KALSHI_EVENT,
        _FEE,
    )
    assert market.terms.comparator is Comparator.LESS
    assert market.terms.threshold == pytest.approx(80.0)


def test_kalshi_between_strike_carries_both_bounds() -> None:
    market = normalize_kalshi_market(
        {**_KALSHI_CRYPTO, "strike_type": "between", "floor_strike": 86, "cap_strike": 87},
        _KALSHI_EVENT,
        _FEE,
    )
    assert market.terms.comparator is Comparator.BETWEEN
    assert market.terms.threshold == pytest.approx(86.0)
    assert market.terms.threshold_upper == pytest.approx(87.0)


def test_kalshi_structured_market_makes_no_numeric_claim() -> None:
    market = normalize_kalshi_market(
        {**_KALSHI_CRYPTO, "strike_type": "structured", "floor_strike": None},
        _KALSHI_EVENT,
        _FEE,
    )
    assert market.terms.comparator is Comparator.NONE
    assert market.terms.threshold is None


def test_kalshi_settlement_sources_flatten_name_and_url() -> None:
    market = normalize_kalshi_market(_KALSHI_CRYPTO, _KALSHI_EVENT, _FEE)
    assert market.terms.resolution_sources == ("cf benchmarks https://www.cfbenchmarks.com/",)


def test_kalshi_reads_the_finest_step_across_every_price_range() -> None:
    """`price_ranges` is not always one flat cent range; reading only entry zero is the bug."""
    tapered = {
        **_KALSHI_CRYPTO,
        "price_ranges": [
            {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
            {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
            {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
        ],
    }
    assert normalize_kalshi_market(tapered, _KALSHI_EVENT, _FEE).tick_dollars == pytest.approx(
        0.001
    )


def test_kalshi_scheduled_start_is_recovered_from_the_rules_prose() -> None:
    """Kalshi publishes a scheduled start only inside `rules_primary`."""
    rules = (
        "If St. Louis wins the Baltimore vs St. Louis professional baseball game originally "
        "scheduled for Aug 25, 2026 at 7:45 PM EDT, then the market resolves to Yes."
    )
    market = normalize_kalshi_market(
        {**_KALSHI_CRYPTO, "rules_primary": rules}, _KALSHI_EVENT, _FEE
    )
    assert market.terms.scheduled_event_time == datetime(2026, 8, 25, 23, 45, tzinfo=UTC)


def test_polymarket_moneyline_normalizes_both_outcomes_and_tokens() -> None:
    market = normalize_polymarket_market(_GAMMA_MONEYLINE)
    assert market is not None
    assert market.yes_label == "Tampa Bay Rays"
    assert market.no_label == "Baltimore Orioles"
    assert (market.yes_token, market.no_token) == ("8088", "6120")
    assert market.terms.scheduled_event_time == datetime(2026, 8, 22, 23, 5, tzinfo=UTC)
    assert market.fee.rate == pytest.approx(0.05)
    # A moneyline makes no numeric claim.
    assert market.terms.threshold is None


def test_polymarket_non_moneyline_types_are_treated_as_line_markets() -> None:
    """`map_handicap` is not in any enumerated list of line types, and must still be one.

    Listing the safe value rather than the unsafe ones is what stops a market type Polymarket
    adds later from silently matching a moneyline.
    """
    for market_type in ("totals", "spreads", "map_handicap", "child_moneyline"):
        market = normalize_polymarket_market(
            {
                **_GAMMA_MONEYLINE,
                "question": "Game Handicap: BLG (-1.5) vs Anyone's Legend (+1.5)",
                "sportsMarketType": market_type,
            }
        )
        assert market is not None, market_type
        assert market.terms.threshold_units == market_type
        assert market.terms.threshold is not None


def test_polymarket_market_without_two_tokens_is_skipped() -> None:
    """A market whose book cannot be located cannot be priced, so it is not returned."""
    assert normalize_polymarket_market({**_GAMMA_MONEYLINE, "clobTokenIds": "[]"}) is None
    assert normalize_polymarket_market({**_GAMMA_MONEYLINE, "outcomes": '["A","B","C"]'}) is None
