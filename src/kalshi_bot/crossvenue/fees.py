"""Taker fee models for each venue, read from the wire rather than assumed.

Fees decide this entire question. A cross-venue pair that costs $0.995 to lock is profitable
before fees and a loser after them, so a fee model that is wrong by a cent inverts the sign of
every opportunity this package reports. `decision/fees.py` already learned that lesson on the
Kalshi side: the fee model is verified against `GET /series/{ticker}` at startup and the poller
refuses to run against a fee type it does not implement. This module keeps that discipline and
extends it to Polymarket.

All three venues charge the same *shape* of fee, which is a genuine convenience: a quadratic in
the traded price, symmetric about 50c, charged to takers, with Kalshi additionally charging a
maker fee on the 133 series whose `fee_type` says so (below). They differ only in where the
coefficient is written on the wire and in how the total is rounded.

- Kalshi: `ceil(0.07 x multiplier x C x P x (1 - P) x 100) / 100`, rounded up to the next
  cent on the whole fill, never per contract. `decision/fair_value.taker_fee()` applies the
  ceiling to a single contract because the EV gate prices one contract at a time; doing that here
  would overstate a 500-contract fill's fee by up to 500x the rounding. `fee_type` and
  `fee_multiplier` come from `GET /series/{ticker}`.
- Polymarket US: `feeCoefficient x C x P x (1 - P)`, rounded to five decimals, taker-only,
  with the coefficient a scalar on the market rather than a nested object. Verified live
  2026-08-22 across all 10,500 open markets: `feeCoefficient` is `0.06` on every one of them.
- Polymarket International: `rate x C x P x (1 - P)`, rounded to five decimals, taker-only,
  with `rate` read per market from Gamma's `feeSchedule` object. Verified live 2026-08-22 across
  100 markets: every schedule carried `exponent: 1` and `takerOnly: true`, `rate` varying by
  category (0.07 crypto, 0.05 sports/economics/culture/weather, 0.04 politics/finance/tech, and
  `feesEnabled: false` for geopolitics, which is fee-free).

`exponent` is the field to watch on the international venue. It is `1` everywhere today, and this
module implements only `1`, and a schedule with any other exponent raises instead of being
silently priced with the wrong curve. That is deliberately a code change and no config toggle,
exactly as `decision/fees.py` handles Kalshi's `fee_type`.

Kalshi's maker fee is modelled too (`maker_rate` on the model): the schedule charges a
resting order `M x 0.0175 x C x P x (1-P)` with M defaulting to 0, and which series carry a
non-zero M is stated by `fee_type` on the live `GET /series` listing, since the schedule PDF is
stale on that membership (`docs/GUIDE.md` §2.7). Polymarket's maker *rebate* (`rebateRate`) remains
unmodelled: a rebate is income conditional on being filled, and pricing it as if fills were free
is the optimistic assumption the falsified-strategy finding warned about.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

from kalshi_bot.crossvenue.venues import Venue

# Kalshi's general-fees rate before the per-series multiplier.
KALSHI_BASE_RATE: Final = 0.07

# Kalshi's maker base rate, from the fee schedule PDF (effective 2026-07-07):
# `fees = round up(M x 0.0175 x C x P x (1-P))`, with the maker M defaulting to 0.
KALSHI_MAKER_BASE_RATE: Final = 0.0175

# The Kalshi `fee_type` values this module implements, mapped to the maker multiplier each
# implies. Verified against the live `GET /series` listing on 2026-08-24 (13,398 series: 13,265
# `quadratic`, 130 `quadratic_with_maker_fees`, 3 `quadratic_with_combo_maker_fees`) and the fee
# schedule PDF: the API's `fee_multiplier` field is the TAKER multiplier; the MAKER multiplier is
# implied by `fee_type` and appears nowhere else on the wire. `flat` exists in the schema, has
# zero live series today, and is a different curve. It stays unimplemented, so a series quoting
# it is skipped instead of priced with the wrong formula.
#
# The previous version of this module implemented only `quadratic`, which silently excluded the
# series that carry maker fees (MLB, NFL, NCAAF, EPL, WNBA, NBA, NHL, UCL and the ATP main tour
# among them) from every consumer that skips unpriceable series. That exclusion had no economic
# rationale: the taker formula for `quadratic_with_maker_fees` is identical to plain `quadratic`.
KALSHI_QUADRATIC_MAKER_MULTIPLIERS: Final[dict[str, float]] = {
    "quadratic": 0.0,
    "quadratic_with_maker_fees": 1.0,
    "quadratic_with_combo_maker_fees": 2.0,
}

# The one Polymarket `feeSchedule.exponent` this module implements.
POLYMARKET_SUPPORTED_EXPONENT: Final = 1

# Polymarket rounds fees to five decimals (docs.polymarket.com, "Fees").
_POLYMARKET_FEE_DECIMALS: Final = 5


class UnsupportedFeeModelError(ValueError):
    """Raised when a venue quotes a fee model this package does not implement.

    A refusal, not a fallback. Pricing an unknown fee curve with a known one produces a number
    that looks like an edge and is not one.
    """


@dataclass(frozen=True, slots=True)
class TakerFeeModel:
    """The taker fee charged on one venue for one market.

    Attributes:
        venue: The venue this model prices.
        rate: Coefficient in `rate x C x P x (1 - P)`. Zero means the market is fee-free.
        round_up_to_cent: Whether the total is rounded up to the next whole cent (Kalshi) or to
            five decimals (Polymarket). This is the only structural difference between the two
            venues' formulas, and on small fills it is not negligible: a 1-contract Kalshi fill at
            2c pays a full cent of fee against a $0.02 premium.
        source: Where the parameters came from, for the dataset's audit trail.
        maker_rate: Coefficient in the maker fee `maker_rate x C x P x (1 - P)`. Zero on
            Kalshi's default fee model, where a resting order pays nothing on 99% of series, and
            `0.0175 x maker_multiplier` where `fee_type` says maker fees apply. Defaults to zero
            so venues without a maker charge (and existing constructions) are unchanged.
    """

    venue: Venue
    rate: float
    round_up_to_cent: bool
    source: str
    maker_rate: float = 0.0

    def fee_dollars_from_basis(self, basis: float) -> float:
        """Total taker fee for a fill whose quadratic basis has already been accumulated.

        `basis` is `sum(contracts_i x price_i x (1 - price_i))` over the levels a fill consumes.
        Accumulating it per level and rounding once is exact; applying `fee_dollars` to a
        volume-weighted average price is not, because `p(1 - p)` is concave and a multi-level
        fill's average price understates the fee at the extremes. Both venues round once per
        fill, so both are priced that way here.

        Args:
            basis: The accumulated quadratic basis. Non-positive values are fee-free.

        Returns:
            The fee in dollars.
        """
        if basis <= 0.0 or self.rate == 0.0:
            return 0.0
        raw = self.rate * basis
        if self.round_up_to_cent:
            return math.ceil(raw * 100.0) / 100.0
        return round(raw, _POLYMARKET_FEE_DECIMALS)

    def fee_dollars(self, price: float, contracts: float) -> float:
        """Total taker fee in dollars for buying `contracts` at `price`.

        Args:
            price: Price per contract in dollars, in `[0, 1]`.
            contracts: Number of contracts (Kalshi) or shares (Polymarket) in the fill. Both
                settle at $1, which is what makes the two venues' quantities comparable.

        Returns:
            The fee in dollars. Never negative.
        """
        return self.fee_dollars_from_basis(contracts * price * (1.0 - price))

    def maker_fee_dollars_from_basis(self, basis: float) -> float:
        """Total maker fee for a fill whose quadratic basis has already been accumulated.

        Same accumulation-then-round-once rule as the taker side, and the same rounding
        convention per venue: Kalshi's schedule states the maker fee as a round-up, so a venue
        with `round_up_to_cent` rounds the maker charge up to the cent too.

        Args:
            basis: The accumulated quadratic basis. Non-positive values are fee-free.

        Returns:
            The fee in dollars. Zero on a free-maker series.
        """
        if basis <= 0.0 or self.maker_rate == 0.0:
            return 0.0
        raw = self.maker_rate * basis
        if self.round_up_to_cent:
            return math.ceil(raw * 100.0) / 100.0
        return round(raw, _POLYMARKET_FEE_DECIMALS)

    def maker_fee_dollars(self, price: float, contracts: float) -> float:
        """Total maker fee in dollars for resting `contracts` filled at `price`.

        Args:
            price: Price per contract in dollars, in `[0, 1]`.
            contracts: Contracts in the fill.

        Returns:
            The fee in dollars. Never negative; rebates are not modelled here.
        """
        return self.maker_fee_dollars_from_basis(contracts * price * (1.0 - price))


def kalshi_fee_model_from_parts(
    *, ticker: str, fee_type: Any, fee_multiplier: Any, source: str
) -> TakerFeeModel:
    """Build Kalshi's fee model from a `(fee_type, fee_multiplier)` pair, wherever it was read.

    Two wire locations quote this pair: `GET /series` (the per-series base) and `GET
    /events/fee_changes` (per-event scheduled overrides, whose `fee_type_override` /
    `fee_multiplier_override` replace the base from `scheduled_ts` on). One constructor serves both
    so the override path cannot drift from the base path.

    Args:
        ticker: The series or event ticker, for error messages and the audit trail.
        fee_type: The wire `fee_type`. Only the three quadratic values are implemented; `flat`
            (zero live series today) is a different curve and is refused.
        fee_multiplier: The wire `fee_multiplier`, the taker multiplier. The maker
            multiplier is implied by `fee_type` (`KALSHI_QUADRATIC_MAKER_MULTIPLIERS`); the API
            carries it nowhere else.
        source: Where the parameters came from.

    Returns:
        The model.

    Raises:
        UnsupportedFeeModelError: On an unimplemented `fee_type` or an unparseable multiplier.
            A fee model that cannot be read cannot be priced.
    """
    maker_multiplier = KALSHI_QUADRATIC_MAKER_MULTIPLIERS.get(str(fee_type or ""))
    if maker_multiplier is None:
        raise UnsupportedFeeModelError(
            f"Kalshi {ticker!r} has fee_type={fee_type!r}; this module implements only "
            f"{sorted(KALSHI_QUADRATIC_MAKER_MULTIPLIERS)}. Supporting another means teaching "
            "TakerFeeModel the formula, not widening a config value."
        )
    try:
        multiplier = float(fee_multiplier)
    except (TypeError, ValueError) as exc:
        raise UnsupportedFeeModelError(
            f"Kalshi {ticker!r} has unparseable fee_multiplier={fee_multiplier!r}"
        ) from exc
    return TakerFeeModel(
        venue=Venue.KALSHI,
        rate=KALSHI_BASE_RATE * multiplier,
        round_up_to_cent=True,
        source=source,
        maker_rate=KALSHI_MAKER_BASE_RATE * maker_multiplier,
    )


def kalshi_fee_model(series: dict[str, Any]) -> TakerFeeModel:
    """Build Kalshi's fee model from a live `GET /series/{ticker}` payload.

    Args:
        series: The `series` object from the response, or the response itself.

    Returns:
        The model for every market in that series, with taker rate from `fee_multiplier` and
        maker rate implied by `fee_type`.

    Raises:
        UnsupportedFeeModelError: If `fee_type` is unimplemented or `fee_multiplier` is missing
            or unparseable.
    """
    body = series.get("series", series)
    fee_type = body.get("fee_type")
    return kalshi_fee_model_from_parts(
        ticker=str(body.get("ticker")),
        fee_type=fee_type,
        fee_multiplier=body.get("fee_multiplier"),
        source=(
            f"GET /series/{body.get('ticker')} fee_type={fee_type} "
            f"multiplier={body.get('fee_multiplier')}"
        ),
    )


def polymarket_fee_model(market: dict[str, Any]) -> TakerFeeModel:
    """Build Polymarket's taker fee model from a Gamma market payload.

    Args:
        market: One market object from `GET /markets`.

    Returns:
        The model for that market. A market with `feesEnabled` false, or with no `feeSchedule`
        at all, is priced fee-free. That is what the wire means, and it is what geopolitics
        markets actually are.

    Raises:
        UnsupportedFeeModelError: If the schedule's `exponent` is anything but 1, or its `rate` is
            unparseable.
    """
    schedule = market.get("feeSchedule")
    fee_type = market.get("feeType")
    if not market.get("feesEnabled") or not isinstance(schedule, dict):
        return TakerFeeModel(
            venue=Venue.POLYMARKET_INTL,
            rate=0.0,
            round_up_to_cent=False,
            source=f"gamma feesEnabled={market.get('feesEnabled')!r} feeType={fee_type!r}",
        )

    exponent = schedule.get("exponent")
    if exponent != POLYMARKET_SUPPORTED_EXPONENT:
        raise UnsupportedFeeModelError(
            f"Polymarket feeType={fee_type!r} has exponent={exponent!r}; this module implements "
            f"only exponent {POLYMARKET_SUPPORTED_EXPONENT}. Any other exponent is a different "
            "fee curve and must be implemented, not assumed."
        )
    try:
        rate = float(schedule["rate"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UnsupportedFeeModelError(
            f"Polymarket feeType={fee_type!r} has unparseable rate={schedule.get('rate')!r}"
        ) from exc
    return TakerFeeModel(
        venue=Venue.POLYMARKET_INTL,
        rate=rate,
        round_up_to_cent=False,
        source=f"gamma feeType={fee_type} rate={rate:g} exponent={exponent}",
    )


def polymarket_us_fee_model(market: dict[str, Any]) -> TakerFeeModel:
    """Build Polymarket US's taker fee model from a `gateway.polymarket.us` market payload.

    Polymarket US states its fee as a single scalar, `feeCoefficient`, rather than the nested
    `feeSchedule` object the international Gamma API returns. Verified live 2026-08-22 across all
    10,500 open markets: every one carried `feeCoefficient: 0.06`, with no exceptions and no
    per-category variation, unlike the international venue, whose rate ranges 0.04 to 0.07 and
    is zero for geopolitics. The coefficient is still read per market instead of hardcoded,
    because a uniform value observed on one day is an observation, not a guarantee.

    There is no `exponent` field to check, and no `feesEnabled` flag: the quadratic shape is
    implicit. That is a smaller surface than Gamma's and so a smaller thing to get wrong.

    Args:
        market: One market object from `GET /v1/markets` or nested inside `GET /v1/events`.

    Returns:
        The taker model for that market.

    Raises:
        UnsupportedFeeModelError: If `feeCoefficient` is absent or unparseable. A missing
            coefficient is refused instead of defaulted to the 0.06 seen everywhere else, the
            same rule the rest of this module follows, because a market that stopped reporting
            its fee is exactly the market whose fee changed.
    """
    raw = market.get("feeCoefficient")
    try:
        rate = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise UnsupportedFeeModelError(
            f"Polymarket US market {market.get('slug')!r} has unparseable "
            f"feeCoefficient={raw!r}; a fee that cannot be read cannot be priced."
        ) from exc
    if rate < 0.0:
        raise UnsupportedFeeModelError(
            f"Polymarket US market {market.get('slug')!r} reports a negative "
            f"feeCoefficient={rate!r}. The maker rebate is a separate number and this package "
            "prices taking only."
        )
    return TakerFeeModel(
        venue=Venue.POLYMARKET_US,
        rate=rate,
        round_up_to_cent=False,
        source=f"gateway /v1/markets feeCoefficient={rate:g}",
    )


FEE_FREE_KALSHI: Final = TakerFeeModel(
    venue=Venue.KALSHI, rate=0.0, round_up_to_cent=True, source="explicit fee-free"
)
