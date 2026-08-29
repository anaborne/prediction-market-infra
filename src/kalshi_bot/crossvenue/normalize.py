"""Lowering each venue's raw payload into `NormalizedMarket`.

One module, one function per venue, and every wire-field access in the package lives in one of
them. That
is deliberate: `ingest/strike_ladder.py` raised `KeyError` on every live call for three phases
while 169 tests passed, because the field names were spread through the code and the fakes
encoded the same misreading (`ENGINEERING.md`, "Wire formats"). Concentrating the reads here means a
divergence is one file to fix and one file to check against a printed response.

Every field name below was read off a live response on 2026-08-22, not off a schema.

Kalshi's strike vocabulary, verified against `KXHIGHNY`, which quotes all three shapes in one
event:

| `strike_type` | fields | quoted as | means |
|---|---|---|---|
| `greater` | `floor_strike=87` | "88° or above" | strictly `> 87` |
| `less` | `cap_strike=80` | "79° or below" | strictly `< 80` |
| `between` | `floor_strike=86`, `cap_strike=87` | "86° to 87°" | inclusive both ends |
| `structured` | none | "Chicago WS wins" | an outcome, no numeric claim |
| `custom` | none | varies | an outcome, no numeric claim |

That `greater` is *strict* against `floor_strike` is the detail that makes `> 86749.99` and
`>= 86750` the same contract, and it is why `Comparator` distinguishes strictness at all.

Polymarket International's shape is a two-outcome market with one CLOB token per outcome.
`outcomes[0]` is not reliably the affirmative, since for a head-to-head it is simply one of the
two teams, so this module records both labels and lets `matching.py` decide which corresponds to
Kalshi's YES.

Polymarket US looks similar and is read completely differently. It also returns `outcomes` and
`outcomePrices` as JSON arrays inside JSON strings, and reading them the way the international
venue's are read is wrong: the two arrays disagree with each other on 36% of open markets, with
`outcomePrices` tracking `marketSides` and `outcomes` reversed against it. That venue's labels,
prices and sides come from `marketSides` alone. It is the third time in this repository that a
field name shared across two APIs meant two different things, and the reason every wire read lives
in this one file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import orjson

from kalshi_bot.crossvenue.fees import (
    TakerFeeModel,
    polymarket_fee_model,
    polymarket_us_fee_model,
)
from kalshi_bot.crossvenue.models import Comparator, NormalizedMarket, SettlementTerms
from kalshi_bot.crossvenue.text import extract_comparator, parse_datetime_phrase, parse_number
from kalshi_bot.crossvenue.venues import Venue

# Kalshi `strike_type` to comparator. `structured` and `custom` carry no numeric claim.
_KALSHI_STRIKE_COMPARATORS: Final[dict[str, Comparator]] = {
    "greater": Comparator.GREATER,
    "greater_or_equal": Comparator.GREATER_EQUAL,
    "less": Comparator.LESS,
    "less_or_equal": Comparator.LESS_EQUAL,
    "between": Comparator.BETWEEN,
}

# The only Polymarket sports market type that is a straight "who wins". Every other value
# (`totals`, `spreads`, `map_handicap`, `game_handicap`, and whatever the venue adds next) is a
# derivative whose line lives in the question text. Listing the *safe* value instead of the
# unsafe ones means a new market type Polymarket introduces is treated as a line market by
# default instead of silently matching a moneyline, which is how `map_handicap` slipped past a
# first attempt that enumerated {"totals", "spreads"}.
_MONEYLINE_MARKET_TYPES: Final[frozenset[str]] = frozenset({"moneyline", ""})

# Side labels that identify nothing. A market labelled this way states its claim in `title`.
_GENERIC_SIDE_LABELS: Final[frozenset[str]] = frozenset({"yes", "no"})

# Polymarket US `marketType` values that are a straight outcome claim, two exhaustive results
# with no line hidden in the question text. Same defensive shape as `_MONEYLINE_MARKET_TYPES`
# above and for the same reason: listing the safe values means a type the venue adds next is
# treated as a line market by default rather than silently matching a Kalshi moneyline.
#
# This reads `marketType`, not `sportsMarketType`, and the difference is not cosmetic. The
# venue quotes 133 distinct `sportsMarketType` values against 7 `marketType` values, and
# `sportsMarketType` is a strict refinement. Measured over 25,513 open markets on 2026-08-22, no
# `sportsMarketType` value maps to more than one `marketType`. An allowlist over the fine-grained
# field therefore has to enumerate 133 values to say what 3 say here, and the first version of
# this constant enumerated 5: it classified `tennis_match_winner`, `table_tennis_match_winner`,
# `esports_match_winner`, `baseball_team_full_game_winner` and eight more as *line markets*, which
# forced a threshold onto about 1,000 genuine head-to-heads. Those questions read "Who will win in
# the upcoming tennis event X vs Y scheduled for August 18, 2026", so `parse_number` returned the
# day of the month as the line, and the settlement gate then refused every one of them for
# claiming a numeric level Kalshi's side did not. That is what a whole-venue scan found: 213 of
# 283 surviving pairs blocked by a threshold read off a date. See `docs/GUIDE.md` §6.
#
# The three safe values, with counts from that read:
#
# - `futures` (11,150), "will X win the championship", a pure outcome claim.
# - `moneyline` (1,186), a head-to-head across 12 different `sportsMarketType` values.
# - `election` (4), "U.S House Midterm Winner" / "Democratic Party", the same shape as futures.
#
# Excluded: `props` (7,403), `spreads` (2,388), `totals` (1,999) state a line in their wording,
# and `drawable_outcome` (1,383, every one a `soccer_team_full_time_winner`) quotes a contest
# whose third result, a draw, means the two named sides are not complements. That last one is
# the mistake `matching._draw_blockers` exists to catch.
_PM_US_OUTCOME_MARKET_TYPES: Final[frozenset[str]] = frozenset(
    {
        "futures",
        "moneyline",
        "election",
    }
)

# Fallback tick when `price_ranges` is missing or unparseable. One cent is Kalshi's coarsest
# published grid, so assuming it never claims a finer grid than the venue offers.
_DEFAULT_KALSHI_TICK: Final = 0.01


def decode_json_array(raw: Any) -> list[Any]:
    """Decode a Gamma field that is a JSON array encoded inside a JSON string.

    Args:
        raw: The field value: a `list` already, a `str` holding a JSON array, or anything else.

    Returns:
        The decoded list, or `[]` when the value is absent or unparseable.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            decoded = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Kalshi returns `close_time`/`expiration_time` as ISO-8601 with a `Z` suffix and does not
    return the `*_ts` epoch-integer counterparts the schema documents (`ENGINEERING.md`). Polymarket
    returns `endDate` the same way and `gameStartTime` as a space-separated offset string.

    Args:
        value: The raw field.

    Returns:
        An aware UTC datetime, or `None` when absent or unparseable.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _kalshi_tick(market: dict[str, Any]) -> float:
    """Read the finest price step this market quotes.

    `price_ranges` is a list of `{start, end, step}` objects and is not always one flat
    one-cent range. The 15-minute crypto series quote `tapered_deci_cent`, with tenth-of-a-cent
    steps in the tails (`ENGINEERING.md`). Reading only `price_ranges[0]["step"]` is the bug that
    section exists to warn about, so this reads every entry.

    Args:
        market: A raw Kalshi market object.

    Returns:
        The smallest step across all ranges, or one cent when unreadable.
    """
    steps: list[float] = []
    for entry in market.get("price_ranges") or []:
        if not isinstance(entry, dict):
            continue
        try:
            step = float(entry["step"])
        except (KeyError, TypeError, ValueError):
            continue
        if step > 0.0:
            steps.append(step)
    return min(steps) if steps else _DEFAULT_KALSHI_TICK


def _kalshi_units(market: dict[str, Any]) -> str | None:
    """Infer what a Kalshi strike counts, from its own subtitle wording.

    Args:
        market: A raw Kalshi market object.

    Returns:
        `"usd"`, `"percent"`, `"bps"`, `"fahrenheit"`, or `None` when nothing in the wording says.
    """
    text = f"{market.get('yes_sub_title') or ''} {market.get('rules_primary') or ''}".lower()
    if "fahrenheit" in text or "°" in text:
        return "fahrenheit"
    if "bps" in text or "basis point" in text:
        return "bps"
    if "percent" in text or "%" in text:
        return "percent"
    if "$" in text or "dollar" in text:
        return "usd"
    return None


def normalize_kalshi_market(
    market: dict[str, Any],
    event: dict[str, Any] | None,
    fee: TakerFeeModel,
) -> NormalizedMarket:
    """Lower one raw Kalshi market into the package's vocabulary.

    Args:
        market: A raw object from `GET /markets`.
        event: The market's event from `GET /events`, or `None`. The event is where `category`,
            `settlement_sources` and `mutually_exclusive` live; none of them appear on the market.
        fee: The series' taker fee model, read from `GET /series/{ticker}`.

    Returns:
        The normalized market.
    """
    event = event or {}
    strike_type = str(market.get("strike_type") or "")
    comparator = _KALSHI_STRIKE_COMPARATORS.get(strike_type, Comparator.NONE)

    threshold: float | None = None
    threshold_upper: float | None = None
    if comparator in (Comparator.GREATER, Comparator.GREATER_EQUAL, Comparator.BETWEEN):
        threshold = _as_float(market.get("floor_strike"))
    elif comparator in (Comparator.LESS, Comparator.LESS_EQUAL):
        threshold = _as_float(market.get("cap_strike"))
    if comparator is Comparator.BETWEEN:
        threshold_upper = _as_float(market.get("cap_strike"))

    rules = str(market.get("rules_primary") or "")
    sources = _flatten_settlement_sources(event.get("settlement_sources"))

    terms = SettlementTerms(
        close_time=_parse_iso(market.get("close_time")),
        expiry_time=_parse_iso(market.get("expected_expiration_time"))
        or _parse_iso(market.get("expiration_time")),
        # Kalshi states a scheduled event's start only inside the rules prose.
        scheduled_event_time=parse_datetime_phrase(rules),
        comparator=comparator,
        threshold=threshold,
        threshold_upper=threshold_upper,
        threshold_units=_kalshi_units(market),
        resolution_sources=sources,
        can_close_early=bool(market.get("can_close_early")),
        raw_rules=rules,
    )

    title = str(market.get("title") or "")
    yes_label = str(market.get("yes_sub_title") or market.get("subtitle") or title)
    return NormalizedMarket(
        venue=Venue.KALSHI,
        market_id=str(market.get("ticker") or ""),
        event_id=str(market.get("event_ticker") or ""),
        question=str(event.get("title") or title),
        yes_label=yes_label,
        no_label=str(market.get("no_sub_title") or f"not {yes_label}"),
        category=str(event.get("category") or "").lower(),
        terms=terms,
        fee=fee,
        tick_dollars=_kalshi_tick(market),
        min_order_contracts=1.0,
        yes_token=str(market.get("ticker") or ""),
        no_token=str(market.get("ticker") or ""),
        claim_text=" ".join(
            part for part in (event.get("title"), title, yes_label) if isinstance(part, str)
        ),
        search_text=" ".join(
            part
            for part in (event.get("title"), title, yes_label, market.get("sub_title"), rules)
            if isinstance(part, str) and part
        ),
    )


def normalize_polymarket_market(market: dict[str, Any]) -> NormalizedMarket | None:
    """Lower one raw Gamma market into the package's vocabulary.

    Args:
        market: A raw object from Gamma's `GET /markets`.

    Returns:
        The normalized market, or `None` when it is not a two-outcome book-enabled market with
        two CLOB token ids. A market missing either token cannot be priced, and a market with
        other than two outcomes is not a binary contract Kalshi could hedge.
    """
    outcomes = [str(value) for value in decode_json_array(market.get("outcomes"))]
    token_ids = [str(value) for value in decode_json_array(market.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(token_ids) != 2:
        return None

    question = str(market.get("question") or "")
    description = str(market.get("description") or "")
    comparator_name = extract_comparator(question)
    comparator = Comparator(comparator_name) if comparator_name != "none" else Comparator.NONE
    threshold = parse_number(question) if comparator is not Comparator.NONE else None
    units = _polymarket_units(question)

    # A totals or spread market states its line in the question text ("Games Total: O/U 3.5",
    # "Spread: Inter Miami CF (-1.5)") and nowhere else, so nothing above extracts it, because
    # "O/U" is not comparator wording. Left alone, such a market reads as having no numeric claim
    # and matches a plain moneyline, which is how the first live scan produced a 23c "arbitrage"
    # between a League of Legends match winner and a handicap on the same match. Tagging the
    # units with the market type makes the threshold gate refuse the cross-type pair outright.
    sports_type = str(market.get("sportsMarketType") or "").lower()
    if sports_type not in _MONEYLINE_MARKET_TYPES:
        comparator = Comparator.GREATER
        threshold = parse_number(question)
        units = sports_type

    events = market.get("events") or []
    event = events[0] if isinstance(events, list) and events else {}
    event_slug = str(event.get("slug") or market.get("slug") or "")

    resolution_source = str(market.get("resolutionSource") or "").lower()
    terms = SettlementTerms(
        # Gamma exposes no separate order-close timestamp; `endDate` is both.
        close_time=_parse_iso(market.get("endDate")),
        expiry_time=_parse_iso(market.get("endDate")),
        scheduled_event_time=_parse_iso(market.get("gameStartTime")),
        comparator=comparator,
        threshold=threshold,
        threshold_upper=None,
        threshold_units=units,
        resolution_sources=(resolution_source,) if resolution_source else (),
        # UMA-resolved markets settle when the oracle does, which may precede `endDate`.
        can_close_early=True,
        raw_rules=description,
    )

    return NormalizedMarket(
        venue=Venue.POLYMARKET_INTL,
        market_id=str(market.get("conditionId") or market.get("id") or ""),
        event_id=event_slug,
        question=question,
        yes_label=outcomes[0],
        no_label=outcomes[1],
        category=_polymarket_category(market),
        terms=terms,
        fee=polymarket_fee_model(market),
        tick_dollars=float(market.get("orderPriceMinTickSize") or 0.01),
        min_order_contracts=float(market.get("orderMinSize") or 1.0),
        yes_token=token_ids[0],
        no_token=token_ids[1],
        claim_text=f"{question} {outcomes[0]}",
        search_text=" ".join(
            part
            for part in (question, outcomes[0], outcomes[1], str(event.get("title") or ""))
            if part
        ),
    )


def normalize_polymarket_us_market(
    market: dict[str, Any],
    event: dict[str, Any] | None = None,
) -> NormalizedMarket | None:
    """Lower one raw Polymarket US market into the package's vocabulary.

    This reads `marketSides` and never `outcomes`. Both venues encode a JSON array inside a
    JSON string here, but on Polymarket US the two label arrays disagree with each other:
    `outcomePrices[i]` tracks `marketSides[i]`, while `outcomes[i]` is reversed relative to it on
    3,813 of the 10,500 open markets measured on 2026-08-22. Zipping `outcomes` against
    `outcomePrices` (the reading `normalize_polymarket_market` performs for the international
    venue, where it is correct) would therefore invert YES and NO on 36% of this venue's
    markets. `polymarket_us_public.py` documents the measurement.

    `marketSides` carries the label, the price, the long flag and tradability in one
    self-consistent object, with index 0 long and index 1 short on every open market observed.

    Whether a market is a line market is decided by `marketType`, not `sportsMarketType`. See
    `_PM_US_OUTCOME_MARKET_TYPES` for why classifying on the finer field silently turned 1,000
    head-to-heads into level markets whose "threshold" was a date.

    This needs the event read, not the flat market listing. On this venue `question` is the
    *event's* claim, shared verbatim across every sibling market: all fifteen legs of "National
    League Champion" carry that same question, and the team is in the market's `title`. Measured
    on 2026-08-22 over 6,036 markets nested inside 500 open events, 5,838 shared their question
    with a sibling and 6,027 labelled their sides a bare "Yes"/"No", so a claim built from
    `question` and the side label alone is identical across a whole event, and the matcher would
    be choosing between the Dodgers and the Braves on a string that does not mention either. That
    is the same failure `NormalizedMarket.claim_text` documents for Kalshi's Netflix rankings.

    `title` is populated on all 6,036 and is the discriminator, so it is folded into the labels
    and the claim text. A scan should still read `polymarket_us_public.iter_open_events`, and not
    because the flat listing omits `title` (an earlier version of this docstring said so and was
    wrong; `docs/GUIDE.md` §6), but because the settlement source this function reads into
    `resolution_sources` lives on the *event* and appears in neither listing's market rows.

    Args:
        market: A raw market object from `GET /v1/markets`, or one nested inside
            `GET /v1/events`.
        event: The market's event, when read through `/v1/events`. The event is where `title`,
            `seriesSlug` and the league's settlement URL live; none of them appear on a market.

    Returns:
        The normalized market, or `None` when it does not have exactly two sides. A market with
        any other shape is not a binary contract Kalshi could hedge, and the venue quoted exactly
        two on all 10,500 open markets, so anything else is a shape this function has not seen
        and must not guess at.
    """
    event = event or {}
    sides = market.get("marketSides")
    if not isinstance(sides, list) or len(sides) != 2:
        return None
    long_side = str(sides[0].get("description") or "") if isinstance(sides[0], dict) else ""
    short_side = str(sides[1].get("description") or "") if isinstance(sides[1], dict) else ""
    if not long_side or not short_side:
        return None

    question = str(market.get("question") or "")
    title = str(market.get("title") or "").strip()
    description = str(market.get("description") or "")
    long_label, short_label = _pm_us_labels(long_side, short_side, title, question)

    comparator_name = extract_comparator(question)
    comparator = Comparator(comparator_name) if comparator_name != "none" else Comparator.NONE
    threshold = parse_number(question) if comparator is not Comparator.NONE else None
    units = _polymarket_units(question)

    # A spread, total, prop or margin market states its line in the question text and nowhere
    # else, so nothing above extracts it. Tagging the units with the market type makes the
    # threshold gate refuse a cross-type pair outright, which is what stops a handicap matching
    # a plain moneyline, the failure the international normalizer already hit once.
    #
    # Classified on the coarse `marketType`, tagged with the fine `sportsMarketType`. The coarse
    # field decides *whether* this is a line market, because it is the one that says so without
    # enumerating 133 values; the fine one becomes the units, because refusing a
    # `football_team_full_game_spread` against a `football_team_full_game_total` is a distinction
    # worth keeping once both are known to be line markets.
    market_type = str(market.get("marketType") or "").lower()
    if market_type not in _PM_US_OUTCOME_MARKET_TYPES:
        comparator = Comparator.GREATER
        threshold = parse_number(question)
        units = str(market.get("sportsMarketType") or "").lower() or market_type

    return NormalizedMarket(
        venue=Venue.POLYMARKET_US,
        # Books, BBO and settlement are all keyed by slug on this venue. There is no token id
        # per outcome the way the international CLOB has, and no fetch-by-id endpoint.
        market_id=str(market.get("slug") or ""),
        event_id=str(event.get("slug") or event.get("ticker") or ""),
        question=question,
        yes_label=long_label,
        no_label=short_label,
        category=str(market.get("category") or event.get("category") or "").lower(),
        terms=SettlementTerms(
            # The gateway exposes no separate order-close timestamp; `endDate` is both.
            close_time=_parse_iso(market.get("endDate")),
            expiry_time=_parse_iso(market.get("endDate")),
            scheduled_event_time=_parse_iso(market.get("gameStartTime"))
            or _parse_iso(event.get("startTime")),
            comparator=comparator,
            threshold=threshold,
            threshold_upper=None,
            threshold_units=units,
            resolution_sources=_pm_us_settlement_sources(event),
            # No wire field states this either way. Unlike the international venue, Polymarket US
            # is a DCM settling on its own published method rather than through an UMA oracle
            # that can resolve at any time, so `False` is the honest reading of what is known.
            # `matching.py` records this field and does not gate on it, so the choice is
            # informational.
            can_close_early=False,
            raw_rules=description,
        ),
        fee=polymarket_us_fee_model(market),
        tick_dollars=float(market.get("orderPriceMinTickSize") or 0.01),
        # Fractional: 0.01 on 9,829 of 10,500 open markets. A share here divides in a way a
        # Kalshi contract does not, which is why this is a float and not a count.
        min_order_contracts=float(market.get("minimumTradeQty") or 1.0),
        # One book per market, addressed by slug, so both sides carry the same handle, the same
        # arrangement as Kalshi's ticker.
        yes_token=str(market.get("slug") or ""),
        no_token=str(market.get("slug") or ""),
        # The title is included separately only when it is not already the label. When the sides
        # are descriptive it is the other half of the claim ("Los Angeles vs. Tennessee" +
        # "Chargers") and must be kept; when it *supplied* the label, repeating it doubles every
        # one of its tokens and skews the matcher's similarity score.
        claim_text=" ".join(
            part for part in (question, title if title != long_label else "", long_label) if part
        ),
        search_text=" ".join(
            part
            for part in (
                question,
                title if title != long_label else "",
                str(market.get("subtitle") or ""),
                long_label,
                short_label,
                str(event.get("title") or ""),
            )
            if part
        ),
    )


def _pm_us_labels(long_side: str, short_side: str, title: str, question: str) -> tuple[str, str]:
    """Decide what buying each side of a Polymarket US market actually claims.

    Two shapes appear on this venue. A head-to-head labels its sides with the competitors
    ("Chargers"/"Titans"), and those labels are the claim. Everything else, 6,027 of the 6,036
    markets measured on 2026-08-22, labels them a bare "Yes"/"No", which says nothing, and puts
    the claim in the market's `title` instead.

    Args:
        long_side: `marketSides[0].description`.
        short_side: `marketSides[1].description`.
        title: The market's `title`, present only on rows nested inside an event response.
        question: The market's question, which on this venue belongs to the event and is shared
            across siblings.

    Returns:
        `(yes_label, no_label)`. Generic sides are replaced by the title when there is one; when
        there is not, the bare "Yes"/"No" is returned unchanged instead of fabricated from the
        shared question. An uninformative label the matcher will score badly is better than a
        confident one built from a string every sibling also carries.
    """
    if long_side.strip().lower() not in _GENERIC_SIDE_LABELS:
        return long_side, short_side
    if not title or title == question:
        return long_side, short_side
    return title, f"not {title}"


def _pm_us_settlement_sources(event: dict[str, Any]) -> tuple[str, ...]:
    """Read the settlement source a Polymarket US event states.

    The source is not on the market and not at the top of the event either. For a sports event it
    is `primaryTag.league.resolution`, a bare URL such as `https://www.mlb.com/`, the same form
    Kalshi's `settlement_sources[].url` takes, which is what lets `matching._source_blockers`
    compare the two at all. Non-sports events carry no league and so state no source; that is an
    absence the matcher records as a blocker rather than something to invent a value for.

    Args:
        event: A raw event object, possibly empty.

    Returns:
        Lowercased source strings, or `()` when the event states none.
    """
    tag = event.get("primaryTag")
    if not isinstance(tag, dict):
        return ()
    league = tag.get("league")
    if not isinstance(league, dict):
        return ()
    parts = [str(league.get(key) or "") for key in ("name", "resolution")]
    joined = " ".join(part for part in parts if part).strip().lower()
    return (joined,) if joined else ()


def _flatten_settlement_sources(raw: Any) -> tuple[str, ...]:
    """Flatten Kalshi's settlement sources into comparable strings.

    Verified live 2026-08-22: every open event carries `settlement_sources` as a list of
    `{"name": ..., "url": ...}` objects, e.g. `[{"name": "MLB", "url": "https://www.mlb.com/"}]`.
    Both halves are kept, because Polymarket states its own source as a bare URL
    (`"https://www.mlb.com/"`) and the overlap that identifies them as the same source may be in
    either the name or the host.

    Args:
        raw: The event's `settlement_sources` field.

    Returns:
        Lowercased source strings, one per entry, name and URL joined.
    """
    if not isinstance(raw, list):
        return ()
    flattened: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            flattened.append(entry.lower())
        elif isinstance(entry, dict):
            parts = [str(entry.get(key) or "") for key in ("name", "url")]
            joined = " ".join(part for part in parts if part).strip().lower()
            if joined:
                flattened.append(joined)
    return tuple(flattened)


def _polymarket_units(question: str) -> str | None:
    """Infer a Polymarket threshold's units from its question wording.

    Args:
        question: The market question.

    Returns:
        `"usd"`, `"percent"`, `"bps"`, or `None`.
    """
    lowered = question.lower()
    if "bps" in lowered or "basis point" in lowered:
        return "bps"
    if "%" in lowered or "percent" in lowered:
        return "percent"
    if "$" in lowered:
        return "usd"
    return None


def _polymarket_category(market: dict[str, Any]) -> str:
    """Derive a comparable category string.

    Gamma has no plain category column, but `feeType` is one in all but name (`sports_fees_v3`,
    `crypto_fees_v2`, `politics_fees`, `economics_fees`) and it is the field the venue itself
    uses to price the market.

    Args:
        market: A raw Gamma market object.

    Returns:
        A lowercased category word, or `""` when the market is fee-free and so carries no
        `feeType` to read.
    """
    fee_type = str(market.get("feeType") or "")
    if not fee_type:
        return ""
    return fee_type.split("_")[0].lower()


def _as_float(value: Any) -> float | None:
    """Coerce a wire value to float without raising.

    Args:
        value: Any wire value.

    Returns:
        The float, or `None` when absent or unparseable.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
