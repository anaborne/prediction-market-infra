"""A synthetic two-venue market corpus, for benchmarking the matcher without shipping real data.

The matcher's headline property is a narrowing: a full cross of two venues is a number of pairs
with nine zeroes on it, almost all of them absurd, and an IDF-weighted inverted index reduces
that to a few tens of thousands worth scoring. Demonstrating that needs a corpus with the two
properties real market wording has, and nothing else:

1. A heavy head and a long tail of tokens. Every sports market contains "will", "win",
   "game"; only a few contain "spanberger". If every token were equally rare the index would be
   useless, and if every token were equally common it would retrieve the whole corpus. The
   generator therefore composes each question from shared boilerplate plus a small number of
   discriminating proper nouns drawn from a Zipf-like distribution.
2. A known number of planted true pairs, worded differently on each side: the same game
   phrased as Kalshi phrases it and as Polymarket phrases it. Without these, a "0 matches" result
   would be indistinguishable from a matcher that is simply broken, which is the failure mode
   this file exists to rule out.

Everything here is generated from a seed. No file in this module reads a network, a database, or
any recorded market: the corpus is reproducible from the seed alone, on any machine, and its
numbers are therefore about the algorithm rather than about a snapshot of two exchanges.

What this corpus is not. It is not a replica of either venue. Its absolute numbers (pairs
crossed, candidates scored, matches found) are properties of *this generator*, and they are not
the production figures measured on a real whole-venue scan. What transfers is the shape: the
reduction factor the index achieves, and the fact that the settlement gate refuses candidates the
similarity score was willing to accept.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from kalshi_bot.crossvenue.fees import TakerFeeModel
from kalshi_bot.crossvenue.models import Comparator, NormalizedMarket, SettlementTerms
from kalshi_bot.crossvenue.venues import Venue

_KALSHI_FEE = TakerFeeModel(Venue.KALSHI, 0.07, True, "synthetic")
_POLY_FEE = TakerFeeModel(Venue.POLYMARKET_US, 0.06, False, "synthetic")

_EPOCH = datetime(2026, 9, 1, tzinfo=UTC)

# Deliberately mundane and heavily repeated: this is the boilerplate that makes lexical overlap
# alone insufficient, and therefore the thing IDF has to see through.
_KALSHI_BOILERPLATE = (
    "This market will resolve to Yes if the outcome occurs as described in the rules below. "
    "If the event is postponed or cancelled the market will resolve according to the exchange "
    "rulebook. All times are Eastern."
)
_POLY_BOILERPLATE = (
    "This market will resolve Yes if the described outcome occurs. In the event of a postponement "
    "resolution follows the official source. Resolution is final."
)

_CITIES = (
    "austin",
    "boise",
    "buffalo",
    "denver",
    "fresno",
    "helena",
    "juneau",
    "lincoln",
    "madison",
    "mobile",
    "omaha",
    "peoria",
    "reno",
    "spokane",
    "tacoma",
    "wichita",
)
_NICKNAMES = (
    "aardvarks",
    "basilisks",
    "condors",
    "dromedaries",
    "egrets",
    "falconers",
    "gannets",
    "harriers",
    "ibexes",
    "jackdaws",
    "kestrels",
    "lampreys",
    "marlins",
    "narwhals",
    "ocelots",
    "petrels",
    "quokkas",
    "rooks",
    "sturgeons",
    "tarpons",
    "urchins",
    "vipers",
)
_SURNAMES = (
    "abernathy",
    "brackenridge",
    "castellanos",
    "delacroix",
    "eastwick",
    "fairweather",
    "gallowglass",
    "hollingsworth",
    "isenberg",
    "jandreau",
    "kirkpatrick",
    "lindqvist",
    "montrachet",
    "nakagawa",
    "oyelaran",
    "pemberton",
    "quillfeather",
    "rasmussen",
    "stavropoulos",
    "thorvaldsen",
    "ustinova",
    "vanterpool",
    "wetherington",
    "yarborough",
)
_OFFICES = ("governor", "senator", "mayor", "comptroller", "attorney general")
_ASSETS = ("bitcoin", "ethereum", "solana", "gold", "brent crude", "natural gas")


@dataclass(frozen=True, slots=True)
class Corpus:
    """One generated cross-venue corpus.

    Attributes:
        kalshi: The Kalshi-shaped side.
        other: The counterparty-venue-shaped side.
        planted_keys: `(kalshi market id, counterparty market id)` for every planted pair, so a
            caller can separate a recovered plant from a verdict nobody asked for.
        planted_pairs: How many true pairs were planted, worded differently on each side. An
            upper bound on what a correct matcher can find, and the reason a zero result is
            readable as a bug rather than as a finding. Filler markets are drawn from disjoint
            per-venue vocabularies, so any identical pair the matcher reports beyond these is a
            defect rather than a coincidence of the generator.
    """

    kalshi: list[NormalizedMarket]
    other: list[NormalizedMarket]
    planted_pairs: int
    planted_keys: frozenset[tuple[str, str]]


def _zipf_pick(rng: random.Random, population: tuple[str, ...]) -> str:
    """Draw from `population` with a Zipf-like bias toward its head.

    Real market vocabularies are not uniform, since a handful of teams and tickers carry most of
    the listings, and a uniform draw would hand the index an easier problem than it actually has.

    Args:
        rng: Seeded source of randomness.
        population: Items to draw from, most common first.

    Returns:
        One item.
    """
    weights = [1.0 / (index + 1) for index in range(len(population))]
    return rng.choices(population, weights=weights, k=1)[0]


def _terms(
    event_time: datetime,
    *,
    comparator: Comparator = Comparator.NONE,
    threshold: float | None = None,
    units: str | None = None,
    sources: tuple[str, ...] = (),
    rules: str = "",
) -> SettlementTerms:
    """Assemble settlement terms for a generated market."""
    return SettlementTerms(
        close_time=event_time,
        expiry_time=event_time + timedelta(hours=3),
        scheduled_event_time=event_time,
        comparator=comparator,
        threshold=threshold,
        threshold_units=units,
        resolution_sources=sources,
        raw_rules=rules,
    )


def _market(
    venue: Venue,
    market_id: str,
    event_id: str,
    question: str,
    yes_label: str,
    no_label: str,
    category: str,
    terms: SettlementTerms,
    boilerplate: str,
) -> NormalizedMarket:
    """Build one market, with `search_text` carrying the boilerplate the index must see through."""
    claim = f"{question} {yes_label}"
    return NormalizedMarket(
        venue=venue,
        market_id=market_id,
        event_id=event_id,
        question=question,
        yes_label=yes_label,
        no_label=no_label,
        category=category,
        terms=terms,
        fee=_KALSHI_FEE if venue is Venue.KALSHI else _POLY_FEE,
        tick_dollars=0.01,
        min_order_contracts=1.0,
        claim_text=claim,
        search_text=f"{claim} {boilerplate} {terms.raw_rules}",
    )


def _game(rng: random.Random, index: int) -> tuple[NormalizedMarket, NormalizedMarket]:
    """A head-to-head planted on both venues, worded as each venue words it.

    Kalshi states a winner market per side ("{Team} vs {Team} Winner", yes label a team name);
    the counterparty asks a question ("Will the {Team} beat the {Team}?"). The wording overlaps
    only on the two nicknames and the shared boilerplate, which is exactly the case the settlement
    gate's scheduled-event-time check is there to resolve.
    """
    home, away = rng.sample(_NICKNAMES, 2)
    city_home, city_away = rng.sample(_CITIES, 2)
    event_time = _EPOCH + timedelta(hours=index % 720)
    sources = ("league official results",)
    kalshi = _market(
        Venue.KALSHI,
        f"KXGAME-{index}-{home.upper()[:4]}",
        f"KXGAME-{index}",
        f"{city_home} {home} vs {city_away} {away} winner",
        f"{city_home} {home}",
        f"{city_away} {away}",
        "sports",
        _terms(event_time, sources=sources, rules=_KALSHI_BOILERPLATE),
        _KALSHI_BOILERPLATE,
    )
    other = _market(
        Venue.POLYMARKET_US,
        f"pm-game-{index}",
        f"pm-event-{index}",
        f"will the {city_home} {home} beat the {city_away} {away}?",
        f"{city_home} {home}",
        f"{city_away} {away}",
        "sports",
        _terms(event_time, sources=sources, rules=_POLY_BOILERPLATE),
        _POLY_BOILERPLATE,
    )
    return kalshi, other


def _venue_pool(population: tuple[str, ...], venue: Venue) -> tuple[str, ...]:
    """Split a vocabulary so the two venues' *filler* markets cannot collide by accident.

    Filler exists to be rejected. If both sides drew from the same names, the generator would
    occasionally emit the same claim on both venues by chance, a genuinely identical pair that
    nobody planted, and the benchmark could no longer say "every IDENTICAL verdict is either a
    planted pair or a bug". Splitting the pool makes that assertion exact. Planted pairs are
    built separately and deliberately share their vocabulary.

    Args:
        population: The full vocabulary.
        venue: Which side is drawing.

    Returns:
        The half of the vocabulary belonging to that side.
    """
    midpoint = len(population) // 2
    return population[:midpoint] if venue is Venue.KALSHI else population[midpoint:]


def _threshold_market(rng: random.Random, index: int, venue: Venue) -> NormalizedMarket:
    """A price-level market, the family where a comparator and a unit decide identity."""
    asset = _zipf_pick(rng, _venue_pool(_ASSETS, venue))
    level = float(rng.randrange(20_000, 140_000, 250))
    event_time = _EPOCH + timedelta(hours=index % 720)
    boilerplate = _KALSHI_BOILERPLATE if venue is Venue.KALSHI else _POLY_BOILERPLATE
    prefix = "KXLEVEL" if venue is Venue.KALSHI else "pm-level"
    return _market(
        venue,
        f"{prefix}-{index}",
        f"{prefix}-event-{index}",
        f"will {asset} close above {level:.0f} usd",
        f"{asset} above {level:.0f}",
        f"{asset} at or below {level:.0f}",
        "crypto",
        _terms(
            event_time,
            comparator=Comparator.GREATER,
            threshold=level,
            units="usd",
            sources=("index provider settlement print",),
            rules=boilerplate,
        ),
        boilerplate,
    )


def _race_market(rng: random.Random, index: int, venue: Venue) -> NormalizedMarket:
    """An election market, the family where one rare proper noun is nearly conclusive."""
    surname = _zipf_pick(rng, _venue_pool(_SURNAMES, venue))
    office = _zipf_pick(rng, _OFFICES)
    city = _zipf_pick(rng, _venue_pool(_CITIES, venue))
    event_time = _EPOCH + timedelta(days=index % 90)
    boilerplate = _KALSHI_BOILERPLATE if venue is Venue.KALSHI else _POLY_BOILERPLATE
    prefix = "KXRACE" if venue is Venue.KALSHI else "pm-race"
    return _market(
        venue,
        f"{prefix}-{index}",
        f"{prefix}-event-{index}",
        f"will {surname} win the {city} {office} race?",
        surname,
        f"not {surname}",
        "politics",
        _terms(event_time, sources=("official canvass",), rules=boilerplate),
        boilerplate,
    )


def build_corpus(
    kalshi_count: int,
    other_count: int,
    *,
    planted_pairs: int = 250,
    seed: int = 20260825,
) -> Corpus:
    """Generate a reproducible cross-venue corpus.

    Args:
        kalshi_count: How many Kalshi-shaped markets to generate, planted pairs included.
        other_count: How many counterparty-venue markets to generate, planted pairs included.
        planted_pairs: How many true pairs to plant on both sides.
        seed: Seed for every random draw, so a run is reproducible from this argument alone.

    Returns:
        The corpus.

    Raises:
        ValueError: If either side is too small to hold the planted pairs.
    """
    if planted_pairs > min(kalshi_count, other_count):
        raise ValueError(
            f"cannot plant {planted_pairs} pairs into sides of {kalshi_count} and {other_count}"
        )
    rng = random.Random(seed)
    kalshi: list[NormalizedMarket] = []
    other: list[NormalizedMarket] = []

    planted_keys: set[tuple[str, str]] = set()
    for index in range(planted_pairs):
        kalshi_side, other_side = _game(rng, index)
        kalshi.append(kalshi_side)
        other.append(other_side)
        planted_keys.add((kalshi_side.market_id, other_side.market_id))

    # The remainder is filler: the same families, independently drawn on each side, so almost
    # nothing lines up. This is what the index has to reject, and it is the overwhelming majority
    # of any real cross.
    builders = (_threshold_market, _race_market)
    for index in range(planted_pairs, kalshi_count):
        kalshi.append(builders[index % 2](rng, index, Venue.KALSHI))
    for index in range(planted_pairs, other_count):
        other.append(builders[index % 2](rng, index + 500_000, Venue.POLYMARKET_US))

    return Corpus(
        kalshi=kalshi,
        other=other,
        planted_pairs=planted_pairs,
        planted_keys=frozenset(planted_keys),
    )
