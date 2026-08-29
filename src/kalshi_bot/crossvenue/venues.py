"""Venue identity and the US-execution policy that governs each one.

This bot runs from the United States. That single constraint decides which venue pairs are
tradeable and which are research-only, and it does not line up with which venues have a
convenient public API, so it is encoded here, in code, instead of left to whoever reads the
scanner output.

Three venues appear in this package:

- Kalshi (`api.elections.kalshi.com`) is a CFTC-designated contract market. A US person may
  trade it. Public market data is unauthenticated; orders are not.
- Polymarket International (`polymarket.com`, `clob.polymarket.com`) is the offshore,
  USDC-on-Polygon venue. It is geoblocked to US IPs and closed to US persons, and this package
  therefore treats it as an *observation-only* price source. Its book is the deepest public
  prediction-market data available and is genuinely useful for building and validating a strategy;
  it is not a leg this operator may fill.
- Polymarket US (QCX LLC, a CFTC-designated contract market acquired by Polymarket in July
  2025 and cleared by the CFTC in September 2025) is the venue a US person actually trades. It
  runs a separate liquidity pool from the international book, so the same question can quote
  differently on each, which is precisely why the international price is not a substitute.

Polymarket US splits its API across two hosts, and conflating them produced a wrong conclusion
once already. `api.polymarket.us` answers `401 "Missing required API key headers"` on every path
including `/health`. It carries orders, portfolio, and both WebSockets, and needs an Ed25519 key.
But the *public* market data lives on `gateway.polymarket.us`, which needs no credentials at all
(`security: []` in its own OpenAPI document): `GET /v1/markets`, `/v1/markets/{slug}/book`,
`/v1/markets/{slug}/bbo`, `/v1/markets/{slug}/settlement`. Verified live 2026-08-22 against real
books with real depth.

So the executable Kalshi <-> Polymarket US pair can be priced from public data. An earlier
version of this module asserted the opposite, having probed only the authenticated host, the same
failure mode as inferring an absent capability from a web UI. See `docs/GUIDE.md` §6.
`polymarket_us_public.py` is the adapter that reads it.

Streaming and order placement still require an API key, which requires KYC through the Polymarket
US iOS app.

`ExecutionPolicy` exists so that any future order path in this repository has one place to ask
"may this operator fill on this venue?" and gets a refusal rather than a comment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Venue(StrEnum):
    """A trading venue this package knows about.

    A `StrEnum` so a venue round-trips through SQLite and JSON as its value without a custom
    converter, which keeps `store.py` free of encoding logic.
    """

    KALSHI = "kalshi"
    POLYMARKET_INTL = "polymarket_intl"
    POLYMARKET_US = "polymarket_us"


class DataAccess(StrEnum):
    """How this package can read a venue's live order book."""

    PUBLIC = "public"
    """Unauthenticated HTTP. Reading the book needs no credentials.

    This describes the *venue*, not this package's coverage of it. Every venue named here is
    `PUBLIC` and every one now has an adapter, but the two facts are independent and the enum
    tracks only the first.
    """

    CREDENTIALED = "credentialed"
    """Requires an API key this repository does not hold. No adapter exists."""


@dataclass(frozen=True, slots=True)
class VenuePolicy:
    """What a US-resident operator may do on one venue.

    Attributes:
        venue: The venue this policy describes.
        us_person_may_trade: Whether a US person may legally hold positions here. `False` makes
            every leg on this venue observation-only, no matter how attractive its quote.
        data_access: How this package reads the venue's book.
        regulator: The regulatory status in one phrase, for operator-facing output.
        reason: Why `us_person_may_trade` is what it is. Surfaced verbatim in refusals so the
            operator sees the reasoning alongside the verdict.
    """

    venue: Venue
    us_person_may_trade: bool
    data_access: DataAccess
    regulator: str
    reason: str


VENUE_POLICIES: Final[dict[Venue, VenuePolicy]] = {
    Venue.KALSHI: VenuePolicy(
        venue=Venue.KALSHI,
        us_person_may_trade=True,
        data_access=DataAccess.PUBLIC,
        regulator="CFTC-designated contract market (KalshiEX LLC)",
        reason=(
            "Kalshi is a CFTC-regulated DCM open to US persons. Some sports-linked contracts are "
            "restricted in individual states; that is a per-contract question this package does "
            "not adjudicate, and an operator must confirm it for their own state before trading "
            "a sports pair."
        ),
    ),
    Venue.POLYMARKET_INTL: VenuePolicy(
        venue=Venue.POLYMARKET_INTL,
        us_person_may_trade=False,
        data_access=DataAccess.PUBLIC,
        regulator="offshore, not registered for US persons",
        reason=(
            "The international Polymarket CLOB is closed to US persons and geoblocked to US IPs. "
            "Its public book is read here as a reference price and a research dataset only. "
            "Filling a leg on it from the US, directly, or through a VPN or a non-US "
            "intermediary, is not something this package supports."
        ),
    ),
    Venue.POLYMARKET_US: VenuePolicy(
        venue=Venue.POLYMARKET_US,
        us_person_may_trade=True,
        data_access=DataAccess.PUBLIC,
        regulator="CFTC-designated contract market (QCX LLC, d/b/a Polymarket US)",
        reason=(
            "Polymarket US is the venue a US person trades, and it runs a separate liquidity "
            "pool from the international book, so its price, not the international one, is the "
            "leg an arbitrage must be priced against. Public market data is unauthenticated on "
            "gateway.polymarket.us; placing orders and streaming both require an Ed25519 API key "
            "obtained after KYC."
        ),
    ),
}


class VenueExecutionError(RuntimeError):
    """Raised when execution is attempted on a venue this operator may not trade."""


def policy_for(venue: Venue) -> VenuePolicy:
    """Return the policy record for `venue`.

    Args:
        venue: The venue to look up.

    Returns:
        Its `VenuePolicy`.

    Raises:
        KeyError: If `venue` has no policy. A venue without a policy is a venue whose legal
            status nobody has decided, which must not silently default to permitted.
    """
    return VENUE_POLICIES[venue]


def us_executable(venue: Venue) -> bool:
    """Whether a US-resident operator may fill an order on `venue`.

    Args:
        venue: The venue to check.

    Returns:
        `True` if a US person may hold positions there.
    """
    return policy_for(venue).us_person_may_trade


def assert_us_executable(venue: Venue) -> None:
    """Refuse execution on a venue closed to US persons.

    Nothing in this read-only package calls this. It exists so that a future order path has an
    obvious, greppable chokepoint, and so the refusal carries the reasoning rather than a bare
    boolean.

    Args:
        venue: The venue an order would be sent to.

    Raises:
        VenueExecutionError: If a US person may not trade on `venue`.
    """
    policy = policy_for(venue)
    if not policy.us_person_may_trade:
        raise VenueExecutionError(
            f"refusing to execute on {venue.value} ({policy.regulator}): {policy.reason}"
        )


def pair_is_us_executable(first: Venue, second: Venue) -> bool:
    """Whether both legs of a two-venue trade could be filled by a US-resident operator.

    An arbitrage is only an arbitrage if both legs fill. A pair with one observation-only venue
    is a measurement, and this package labels it as one.

    Args:
        first: One leg's venue.
        second: The other leg's venue.

    Returns:
        `True` only if both venues are open to US persons.
    """
    return us_executable(first) and us_executable(second)
