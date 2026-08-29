"""Venue-neutral types: normalized markets, books, match candidates, and priced opportunities.

Two venues describe the same contract in incompatible vocabularies. Kalshi quotes a binary market
with a YES and a NO side, both priced from the YES side (the YES-side wire-price rule), and returns
an order book of
resting bids on each side with no ask array at all. Polymarket issues two ERC-1155 outcome
tokens per market and returns a conventional two-sided book per token. Neither shape survives
being compared to the other directly.

So every adapter lowers its venue into the types here, and every downstream module (matching,
arbitrage, storage) reads only these. The normalization that matters:

- A contract is a claim paying $1. Kalshi contracts and Polymarket shares are both that, which
  is what makes their quantities addable and their prices comparable at all.
- Buying is expressed as taking asks. `OutcomeBook.asks` is what it costs to *acquire* an
  outcome, ascending. On Kalshi this is synthesized: there is no YES ask book, only NO bids, and
  buying YES at `p` means matching a resting NO bid at `1 - p`. `kalshi_public.py` does that
  conversion so nothing downstream has to remember it.
- Settlement terms are structured, not prose. `SettlementTerms` carries the comparator, the
  threshold, the resolution sources and the times as fields, because "identical in what they
  trade" is decided on those and not on how the question is worded.

`MatchConfidence` deliberately separates "these are about the same thing" from "these settle
identically". Two markets can be the same question and still not be a hedge: different index,
different tie handling, a settlement two days apart. Only `IDENTICAL` asserts the second, and only
`IDENTICAL` may carry a claimed arbitrage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from kalshi_bot.crossvenue.fees import TakerFeeModel
from kalshi_bot.crossvenue.venues import Venue


class Side(StrEnum):
    """Which side of a binary market an order acquires."""

    YES = "yes"
    NO = "no"


def opposite(side: Side) -> Side:
    """Return the other side of a binary market.

    Args:
        side: A side.

    Returns:
        The complementary side.
    """
    return Side.NO if side is Side.YES else Side.YES


class Comparator(StrEnum):
    """How a market's threshold is compared to the settlement value.

    The distinction between `GREATER` and `GREATER_EQUAL` looks pedantic and is not: Kalshi's
    crypto strikes are quoted as `floor_strike = 86749.99` with `strike_type = "greater"` to
    express the same claim a venue writing `>= 86750` would. Two markets whose thresholds differ
    by one tick but whose comparators differ correspondingly are the *same* contract, and one
    that ignores the comparator will either miss that match or assert a false one.
    """

    GREATER = "greater"
    GREATER_EQUAL = "greater_equal"
    LESS = "less"
    LESS_EQUAL = "less_equal"
    BETWEEN = "between"
    EQUAL = "equal"
    NONE = "none"
    """No numeric threshold: an outcome market ("does X win?"), not a level market."""


@dataclass(frozen=True, slots=True)
class SettlementTerms:
    """The structured settlement criteria of one market.

    Everything here is a fact the matcher compares directly, rather than inferring from wording.

    Attributes:
        close_time: When the venue stops accepting orders, UTC.
        expiry_time: When the market is expected to settle, UTC. On Kalshi this is
            `expected_expiration_time`, which for a scheduled event may be hours after the event
            and days before `expiration_time`; on Polymarket it is `endDate`. The two venues do
            not mean quite the same thing by it, which is why `scheduled_event_time` exists.
        scheduled_event_time: When the underlying real-world event is scheduled to occur, UTC, if
            the venue states it. This is the single most discriminating field for sports: two
            venues word the same game completely differently but agree on its start time to the
            minute.
        comparator: How `threshold` relates to the settlement value.
        threshold: The numeric level, if this is a level market. For `BETWEEN` this is the lower
            bound, and Kalshi's ranges are inclusive at both ends. `floor_strike=86, cap_strike=87`
            is quoted "86° to 87°", verified live 2026-08-22 on `KXHIGHNY`.
        threshold_upper: The upper bound of a `BETWEEN` range, `None` otherwise.
        threshold_units: What `threshold` counts, lowercased ("usd", "bps", "runs"). Two markets
            with the same number in different units are not the same market.
        resolution_sources: The venue's stated settlement sources, lowercased. Kalshi's
            `settlement_sources` and rules text; Polymarket's `resolutionSource` and description.
        can_close_early: Whether the venue may settle before `expiry_time`. A pair where one side
            can close early and the other cannot is not a clean hedge.
        raw_rules: The venue's own rules prose, kept verbatim for the audit trail and for a human
            reading a flagged pair.
    """

    close_time: datetime | None
    expiry_time: datetime | None
    scheduled_event_time: datetime | None = None
    comparator: Comparator = Comparator.NONE
    threshold: float | None = None
    threshold_upper: float | None = None
    threshold_units: str | None = None
    resolution_sources: tuple[str, ...] = ()
    can_close_early: bool = False
    raw_rules: str = ""


@dataclass(frozen=True, slots=True)
class NormalizedMarket:
    """One venue's binary market, lowered into this package's vocabulary.

    Attributes:
        venue: Where it trades.
        market_id: The venue's own identifier, a Kalshi ticker or a Polymarket condition id.
        event_id: The venue's grouping identifier, so sibling strikes of one event are visible as
            siblings. Kalshi's `event_ticker`; Polymarket's event slug.
        question: The venue's headline wording, verbatim.
        yes_label: What buying YES claims, verbatim ("St. Louis", "Tampa Bay Rays", "$86,750 or
            above"). On Polymarket this is `outcomes[0]`.
        no_label: What buying NO claims, verbatim.
        category: The venue's own category string, lowercased, or `""`.
        terms: Structured settlement criteria.
        fee: The taker fee model for this market, read from the wire.
        tick_dollars: Minimum price increment.
        min_order_contracts: Minimum order size the venue accepts.
        yes_token: Venue-specific handle for the YES side's book. Polymarket needs a CLOB token
            id per outcome; Kalshi needs only the ticker. Empty when unused.
        no_token: The same for the NO side.
        claim_text: The venue's most precise statement of *what is claimed*, excluding rules
            boilerplate. Kept separate from `search_text` because the two answer different
            questions and a token that helps one hurts the other: Kalshi's rules for a "#2 US
            Netflix Movie" market say "is #2 on the Netflix Top 10", so the word "top"
            appears in its rules and its counterpart's headline alike, and a check for
            meaning-changing words run over the rules therefore cannot tell rank 1 from rank 2.
            Run over the claim text, it can.
        search_text: Concatenated wording used for candidate generation, verbatim and
            unnormalized. `text.py` owns normalization, and keeping the raw form here means a
            change to normalization does not require refetching.
    """

    venue: Venue
    market_id: str
    event_id: str
    question: str
    yes_label: str
    no_label: str
    category: str
    terms: SettlementTerms
    fee: TakerFeeModel
    tick_dollars: float
    min_order_contracts: float
    yes_token: str = ""
    no_token: str = ""
    claim_text: str = ""
    search_text: str = ""


@dataclass(frozen=True, slots=True)
class BookLevel:
    """One price level.

    Attributes:
        price_dollars: Price per contract, in dollars.
        size_contracts: Contracts resting at that price.
    """

    price_dollars: float
    size_contracts: float


@dataclass(frozen=True, slots=True)
class OutcomeBook:
    """The takeable ladder for one side of one market.

    Attributes:
        asks: What it costs to buy this outcome, ascending by price. Empty means nothing is
            offered, which is not the same as a price of zero, the distinction
            `decision/fair_value.is_tradeable_price` exists for.
        bids: What this outcome can be sold into, descending by price.
    """

    asks: tuple[BookLevel, ...] = ()
    bids: tuple[BookLevel, ...] = ()

    @property
    def best_ask(self) -> float | None:
        """Lowest price at which this outcome can be bought, or `None` if nothing is offered."""
        return self.asks[0].price_dollars if self.asks else None

    @property
    def best_bid(self) -> float | None:
        """Highest price at which this outcome can be sold, or `None` if nothing is bid."""
        return self.bids[0].price_dollars if self.bids else None

    @property
    def ask_depth_contracts(self) -> float:
        """Total contracts offered across every ask level."""
        return sum(level.size_contracts for level in self.asks)


@dataclass(frozen=True, slots=True)
class MarketBook:
    """Both sides of one market's book at one instant.

    Attributes:
        venue: Where it was read.
        market_id: The market it belongs to.
        yes: Ladder for the YES outcome.
        no: Ladder for the NO outcome.
        observed_at_ms: Wall-clock milliseconds when the read completed. Two venues polled
            sequentially are never simultaneous, and the gap is the single largest source of
            phantom edge in a cross-venue scan, so it is recorded and not hidden.
    """

    venue: Venue
    market_id: str
    yes: OutcomeBook
    no: OutcomeBook
    observed_at_ms: int


class MatchConfidence(StrEnum):
    """How strongly two markets are believed to be the same tradeable claim.

    Ordered from strongest to weakest. Only `IDENTICAL` is a hedge.
    """

    IDENTICAL = "identical"
    """Same claim, and every settlement criterion checked agrees. Arbitrage may be claimed."""

    STRONG = "strong"
    """Same claim by wording and structure, but at least one settlement criterion is unverifiable
    from the data, usually a resolution source one venue does not publish. Worth a human read;
    not worth capital."""

    WEAK = "weak"
    """Plausibly related. Recorded so the dataset can be used to improve the matcher, and so a
    near-miss is visible rather than silently dropped."""

    REJECTED = "rejected"
    """Considered and ruled out. Kept with its reason, because the rejections are the training
    signal for everything this matcher currently gets wrong."""


@dataclass(frozen=True, slots=True)
class MatchedPair:
    """Two markets believed to express the same claim, with the evidence for that belief.

    Attributes:
        kalshi: The Kalshi side.
        other: The other venue's side.
        aligned: Whether Kalshi YES corresponds to `other`'s YES (`True`) or to its NO (`False`).
            Polymarket's `outcomes[0]` is not reliably the affirmative, since for a head-to-head
            it is simply one of the two teams, so this is resolved per pair instead of assumed.
        confidence: How strongly the identity is believed.
        score: The matcher's similarity score in `[0, 1]`, before the settlement gate.
        evidence: Human-readable reasons the matcher reached this verdict, in the order it
            found them. This is what makes a flagged pair auditable by a person.
        blockers: Settlement criteria that failed or could not be checked. Empty for `IDENTICAL`.
    """

    kalshi: NormalizedMarket
    other: NormalizedMarket
    aligned: bool
    confidence: MatchConfidence
    score: float
    evidence: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def pair_key(self) -> str:
        """A stable identifier for this pair, for deduplication and dataset joins."""
        return f"{self.kalshi.market_id}|{self.other.venue.value}:{self.other.market_id}"


@dataclass(frozen=True, slots=True)
class ArbLeg:
    """One leg of a priced cross-venue trade.

    Attributes:
        venue: Where the leg would fill.
        market_id: The market.
        side: Which outcome is bought.
        contracts: Quantity, in contracts.
        gross_cost_dollars: Cost of the contracts before fees, walking the real ladder.
        fee_dollars: Taker fee on that fill.
        vwap_dollars: Volume-weighted average fill price, before fees.
        levels_consumed: How many price levels the fill would eat. One level is a normal fill;
            eight is a claim about depth that deserves scepticism.
    """

    venue: Venue
    market_id: str
    side: Side
    contracts: float
    gross_cost_dollars: float
    fee_dollars: float
    vwap_dollars: float
    levels_consumed: int

    @property
    def total_cost_dollars(self) -> float:
        """Cost of this leg including fees."""
        return self.gross_cost_dollars + self.fee_dollars


@dataclass(frozen=True, slots=True)
class ArbOpportunity:
    """A priced, depth-aware cross-venue opportunity on one matched pair.

    A "buy both sides" arbitrage: acquiring one contract of a claim on one venue and one contract
    of its complement on the other guarantees exactly $1 back, whichever way the world goes. The
    opportunity is real when the two legs together cost less than $1 after fees, and only when
    the two markets genuinely settle identically, which is what `MatchedPair.confidence` decides
    and this type does not re-litigate.

    Attributes:
        pair: The matched markets.
        legs: The two legs, in no particular order.
        contracts: Quantity locked, the same on both legs by construction.
        total_cost_dollars: Both legs' cost including both fees.
        payout_dollars: What settlement returns, `contracts x $1`, always.
        profit_dollars: `payout - total_cost`. Negative values are not reported as opportunities
            but are recorded, since the distribution of near-misses is the actual research output.
        edge_per_contract: `profit / contracts`, the number comparable across pairs.
        us_executable: Whether both legs sit on venues a US person may trade. When `False` this
            is a measurement of a spread, not a trade.
        observed_at_ms: When the later of the two books was read.
        book_skew_ms: Milliseconds between the two venues' book reads. Edge smaller than what
            this gap can move is not edge.
    """

    pair: MatchedPair
    legs: tuple[ArbLeg, ArbLeg]
    contracts: float
    total_cost_dollars: float
    payout_dollars: float
    profit_dollars: float
    edge_per_contract: float
    us_executable: bool
    observed_at_ms: int
    book_skew_ms: int
    notes: tuple[str, ...] = field(default_factory=tuple)


def sorted_levels(levels: tuple[BookLevel, ...], *, ascending: bool) -> tuple[BookLevel, ...]:
    """Order one side of a book best-first.

    Every venue this package reads sorts its book differently, and at least one of them sorts it
    differently from what its own documentation implies: Polymarket International returns the
    touch last, Polymarket US returns it first, and Kalshi returns resting bids ascending
    on both sides. Sorting explicitly at the adapter boundary rather than trusting any of those
    is what stops a silent reordering upstream from inverting every price this package reports.

    Args:
        levels: Levels in whatever order the wire gave them.
        ascending: `True` for an ask ladder (cheapest first), `False` for a bid ladder (highest
            first).

    Returns:
        The same levels, best-first.
    """
    ordered = sorted(levels, key=lambda level: level.price_dollars, reverse=not ascending)
    return tuple(ordered)


def complement_ladder(bids: tuple[BookLevel, ...]) -> tuple[BookLevel, ...]:
    """Turn one side's resting bids into the opposite side's ask ladder.

    Both Kalshi and Polymarket US quote a binary market from a single side (Kalshi from YES,
    Polymarket US from long) and neither publishes an ask array for the complement. A resting
    bid at `p` for one outcome is an offer to sell the other outcome at `1 - p`, for the same
    size. Verified on Polymarket US against the same market's own top-of-book fields: with the
    best long bid at `0.0700`, the venue reported `shortQuote: "0.93"`.

    This is the only place in the package that conversion happens, so a mistake in it is one
    mistake rather than one per venue.

    Args:
        bids: Resting bids on one side, in any order.

    Returns:
        The opposite side's asks, ascending by price. Levels whose complement falls at or outside
        `(0, 1)` are dropped: nothing at or above par is takeable, and a `0.0000` level is an
        empty book rather than a free contract.
    """
    asks = [
        BookLevel(price_dollars=1.0 - level.price_dollars, size_contracts=level.size_contracts)
        for level in bids
        if 0.0 < 1.0 - level.price_dollars < 1.0
    ]
    return sorted_levels(tuple(asks), ascending=True)
