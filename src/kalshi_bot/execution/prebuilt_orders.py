"""Pre-built order templates.

Defines the shape of orders that can be prepared ahead of time and filled in quickly on the hot
path, to minimize per-order construction latency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

_VALID_OUTCOME_SIDES: Final = ("yes", "no")


@dataclass(frozen=True, slots=True)
class PrebuiltOrderTemplate:
    """A partially-constructed order shape awaiting hot-path fill-in.

    Kalshi's current order-creation endpoint (`POST /portfolio/events/orders`) expresses order
    direction as one outcome choice at a price, not a buy/sell action layered on top of a yes/no
    side. See `docs/GUIDE.md`. There is accordingly no `action` field here.

    Attributes:
        ticker: Market ticker the template applies to.
        outcome_side: Which outcome the order takes a position in, "yes" or "no".
    """

    ticker: str
    outcome_side: str


def build_template(ticker: str, outcome_side: str) -> PrebuiltOrderTemplate:
    """Construct a `PrebuiltOrderTemplate` for later hot-path use.

    Validates `outcome_side` against the same literal set `telemetry/schema.sql` enforces
    (`CHECK (outcome_side IN ('yes', 'no'))`), so a bad value fails here, at startup and off the
    hot path, instead of only surfacing after a round trip to Kalshi or at the telemetry write's
    `CHECK` constraint.

    Args:
        ticker: Market ticker the template applies to.
        outcome_side: Which outcome the order takes a position in, "yes" or "no".

    Returns:
        A `PrebuiltOrderTemplate` ready to be filled in and dispatched.

    Raises:
        ValueError: If `outcome_side` is not one of the values `schema.sql` allows.
    """
    if outcome_side not in _VALID_OUTCOME_SIDES:
        raise ValueError(
            f"outcome_side must be one of {_VALID_OUTCOME_SIDES}, got {outcome_side!r}"
        )
    return PrebuiltOrderTemplate(ticker=ticker, outcome_side=outcome_side)
