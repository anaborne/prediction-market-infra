"""Tests for cross-venue market matching and the settlement gate.

Every rejection case below is a false positive a live scan actually produced on 2026-08-22, with
the real wording that produced it. They are regression tests in the strict sense: each one priced
out as a large, entirely fictional "arbitrage" before the corresponding check existed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kalshi_bot.crossvenue.fees import TakerFeeModel
from kalshi_bot.crossvenue.matching import MarketMatcher, MatcherConfig
from kalshi_bot.crossvenue.models import (
    Comparator,
    MatchConfidence,
    NormalizedMarket,
    SettlementTerms,
)
from kalshi_bot.crossvenue.venues import Venue

_KALSHI_FEE = TakerFeeModel(Venue.KALSHI, 0.07, True, "test")
_POLY_FEE = TakerFeeModel(Venue.POLYMARKET_INTL, 0.05, False, "test")

_GAME_START = datetime(2026, 8, 25, 23, 45, tzinfo=UTC)


def _kalshi(
    market_id: str,
    question: str,
    yes_label: str,
    *,
    rules: str = "",
    start: datetime | None = _GAME_START,
    sources: tuple[str, ...] = ("mlb https://www.mlb.com/",),
    comparator: Comparator = Comparator.NONE,
    threshold: float | None = None,
    units: str | None = None,
    market_title: str = "",
) -> NormalizedMarket:
    claim = " ".join(part for part in (question, market_title, yes_label) if part)
    return NormalizedMarket(
        venue=Venue.KALSHI,
        market_id=market_id,
        event_id=market_id.rsplit("-", 1)[0],
        question=question,
        yes_label=yes_label,
        no_label=f"not {yes_label}",
        category="sports",
        terms=SettlementTerms(
            close_time=_GAME_START,
            expiry_time=_GAME_START,
            scheduled_event_time=start,
            comparator=comparator,
            threshold=threshold,
            threshold_units=units,
            resolution_sources=sources,
            raw_rules=rules,
        ),
        fee=_KALSHI_FEE,
        tick_dollars=0.01,
        min_order_contracts=1.0,
        claim_text=claim,
        search_text=f"{claim} {rules}",
    )


def _poly(
    market_id: str,
    question: str,
    yes_label: str,
    no_label: str,
    *,
    start: datetime | None = _GAME_START,
    sources: tuple[str, ...] = ("https://www.mlb.com/",),
    comparator: Comparator = Comparator.NONE,
    threshold: float | None = None,
    units: str | None = None,
    event_title: str = "",
) -> NormalizedMarket:
    return NormalizedMarket(
        venue=Venue.POLYMARKET_INTL,
        market_id=market_id,
        event_id=market_id,
        question=question,
        yes_label=yes_label,
        no_label=no_label,
        category="sports",
        terms=SettlementTerms(
            close_time=_GAME_START,
            expiry_time=_GAME_START,
            scheduled_event_time=start,
            comparator=comparator,
            threshold=threshold,
            threshold_units=units,
            resolution_sources=sources,
            can_close_early=True,
        ),
        fee=_POLY_FEE,
        tick_dollars=0.01,
        min_order_contracts=5.0,
        yes_token=f"{market_id}-yes",
        no_token=f"{market_id}-no",
        claim_text=f"{question} {yes_label}",
        search_text=f"{question} {yes_label} {no_label} {event_title}",
    )


# Unrelated markets on both sides, so token frequencies mean something. A two-document corpus
# gives every token an IDF of zero and makes the weighting degenerate, which is realistic only
# in a test, since a live scan compares ~98,000 Kalshi markets to a few thousand Polymarket ones.
_FILLER_SUBJECTS = (
    "Bitcoin price above 90000",
    "Government shutdown before October",
    "Hurricane landfall in Florida",
    "Nobel Peace Prize laureate announced",
    "Fed cuts rates by 50bps",
    "Oscar for Best Picture",
    "Manchester City wins the league",
    "SpaceX Starship orbital flight",
)


def _filler_markets() -> tuple[list[NormalizedMarket], list[NormalizedMarket]]:
    kalshi = [
        _kalshi(f"KXFILLER-{index}", subject, subject, start=None)
        for index, subject in enumerate(_FILLER_SUBJECTS)
    ]
    other = [
        _poly(f"0xfiller{index}", subject, "Yes", "No", start=None)
        for index, subject in enumerate(_FILLER_SUBJECTS)
    ]
    return kalshi, other


def _match(
    kalshi: NormalizedMarket, other: NormalizedMarket, config: MatcherConfig | None = None
) -> object:
    filler_kalshi, filler_other = _filler_markets()
    pairs = MarketMatcher(config).match_all([kalshi, *filler_kalshi], [other, *filler_other])
    for pair in pairs:
        if pair.kalshi.market_id == kalshi.market_id and pair.other.market_id == other.market_id:
            return pair
    raise AssertionError("expected the matcher to score this pair at all")


def test_head_to_head_matches_across_naming_conventions() -> None:
    """Kalshi's city-plus-initials label must resolve to Polymarket's full team name."""
    kalshi = _kalshi(
        "KXMLBGAME-26AUG251945NYMCWS-CWS",
        "New York M vs Chicago WS",
        "Chicago WS",
        rules="If Chicago WS wins the New York M vs Chicago WS professional baseball game",
    )
    other = _poly(
        "0xabc",
        "New York Mets vs. Chicago White Sox",
        "New York Mets",
        "Chicago White Sox",
    )
    pair = _match(kalshi, other)
    assert pair.confidence is MatchConfidence.IDENTICAL  # type: ignore[attr-defined]
    # Kalshi's YES is the White Sox, which is Polymarket's *second* outcome.
    assert pair.aligned is False  # type: ignore[attr-defined]


def test_proposition_market_aligns_to_yes() -> None:
    kalshi = _kalshi(
        "KXPRESNOMR-28-COWE",
        "2028 Republican presidential nominee",
        "Candace Owens",
        sources=("ap https://apnews.com/",),
        start=None,
    )
    other = _poly(
        "0xdef",
        "Will Candace Owens win the 2028 Republican presidential nomination?",
        "Yes",
        "No",
        sources=("https://apnews.com/",),
        start=None,
    )
    pair = _match(kalshi, other)
    assert pair.aligned is True  # type: ignore[attr-defined]
    assert pair.confidence is MatchConfidence.IDENTICAL  # type: ignore[attr-defined]


def test_vice_presidential_market_is_rejected_against_presidential() -> None:
    """`KXVPRESNOMD` scored 0.44 against the presidential question on a live scan.

    Every word but the office matched. Seven points of lexical separation is not a margin worth
    trusting, so a one-sided qualifier is a rejection rather than a score penalty.
    """
    kalshi = _kalshi("KXVPRESNOMD-28-JPOL", "2028 Democratic VP nominee", "Jared Polis", start=None)
    other = _poly(
        "0x111",
        "Will Jared Polis win the 2028 Democratic presidential nomination?",
        "Yes",
        "No",
        start=None,
    )
    pair = _match(kalshi, other)
    assert pair.confidence is MatchConfidence.REJECTED  # type: ignore[attr-defined]
    assert "vp" in pair.blockers[0]  # type: ignore[attr-defined]


def test_map_winner_is_rejected_against_match_winner() -> None:
    """Kalshi quotes a per-map winner beside Polymarket's whole-match winner.

    These produced eight of twenty "opportunities" on a live scan. The distinguishing word is in
    each venue's claim text and nowhere else.
    """
    kalshi = _kalshi(
        "KXCS2MAP-26AUG230730TSFUT-3-FUT",
        "FUT Esports vs. Spirit: Map 3",
        "FUT Esports",
        market_title="Will FUT Esports win map 3 in the FUT Esports vs. Spirit match?",
    )
    other = _poly(
        "0x222",
        "Counter-Strike: FUT Esports vs Spirit (BO5) - Esports",
        "FUT Esports",
        "Spirit",
    )
    pair = _match(kalshi, other)
    assert pair.confidence is MatchConfidence.REJECTED  # type: ignore[attr-defined]
    assert "map" in pair.blockers[0]  # type: ignore[attr-defined]


def test_rank_two_market_is_rejected_against_rank_one() -> None:
    """Kalshi's "#2 US Netflix Movie" priced 74c of phantom edge against "the top US Netflix".

    Kalshi's *rules* say "is #2 on the Netflix Top 10", so both sides contain "top" and the check
    only works when scoped to the claim text.
    """
    kalshi = _kalshi(
        "KXNETFLIXRANKMOVIERUNNERUP-26AUG24-DON",
        "#2 US Netflix Movie this week?",
        "Don't Say Good Luck",
        rules="If Don't Say Good Luck is #2 on the Netflix Top 10 US Movie chart",
        start=None,
        sources=("netflix https://www.netflix.com/",),
    )
    other = _poly(
        "0x333",
        'Will "Don\'t Say Good Luck" be the top US Netflix movie this week?',
        "Yes",
        "No",
        start=None,
        sources=("https://www.netflix.com/",),
    )
    pair = _match(kalshi, other)
    assert pair.confidence is MatchConfidence.REJECTED  # type: ignore[attr-defined]
    assert "top" in pair.blockers[0]  # type: ignore[attr-defined]


def test_draw_capable_contest_with_two_competitor_outcomes_is_blocked() -> None:
    """A cricket test match can draw, so the two named results are not complements.

    A live scan paired Kalshi's "Australia wins" with Polymarket's Bangladesh outcome and priced
    87c of edge on a book where a third outcome, the draw, was quoted separately.
    """
    kalshi = _kalshi(
        "KXTESTMATCH-26AUG212000BANAUS-AUS",
        "Bangladesh vs Australia",
        "Australia",
        rules="If Australia wins the Bangladesh vs Australia cricket test match",
    )
    other = _poly("0x444", "Bangladesh vs. Australia", "Bangladesh", "Australia")
    pair = _match(kalshi, other)
    assert pair.confidence is not MatchConfidence.IDENTICAL  # type: ignore[attr-defined]


def test_kbo_and_npb_games_are_blocked_because_they_can_tie() -> None:
    """A tie in Korean or Japanese baseball makes both legs of the hedge pay zero.

    Every one of the 33 pairs the first Polymarket US scan passed as `IDENTICAL` was a KBO or NPB
    game. Both leagues end a regular-season game in a tie once extra innings run out, and Kalshi
    words each side "If {team} wins ... resolves to Yes", so a tie resolves NO on both of its
    markets. Buying Kalshi's YES and the counterparty's complement then loses the entire position
    instead of merely failing to lock a profit, which is worse than the soccer case this check
    was built for. Neither venue writes "draw" anywhere in a baseball game's rules, so the
    wording check had nothing to catch until the league names were added to it.
    """
    for series, league, teams in (
        ("KXKBOGAME-26AUG230600LGHAN-HAN", "KBO", ("Hanwha Eagles", "LG Twins")),
        ("KXNPBGAME-26AUG230300SAITOH-SAI", "NPB", ("Saitama Seibu Lions", "Tohoku Rakuten")),
    ):
        kalshi = _kalshi(
            series,
            f"{teams[0]} vs {teams[1]}",
            teams[0],
            rules=(
                f"If {teams[0]} wins the {teams[0]} vs {teams[1]} "
                f"{'Korea' if league == 'KBO' else 'Japan'} {league} game originally scheduled "
                "for Aug 23, 2026, then the market resolves to Yes."
            ),
        )
        other = _poly("0x777", f"{teams[1]} vs {teams[0]}", teams[1], teams[0])
        pair = _match(kalshi, other)
        assert pair.confidence is not MatchConfidence.IDENTICAL  # type: ignore[attr-defined]


def test_moneyline_against_a_totals_line_is_blocked() -> None:
    """A totals line is a different contract from a match winner, whatever the wording shares."""
    kalshi = _kalshi("KXMLBGAME-26AUG251945BALSTL-STL", "Baltimore vs St. Louis", "St. Louis")
    other = _poly(
        "0x555",
        "Baltimore Orioles vs. St. Louis Cardinals: O/U 8.5",
        "Over",
        "Under",
        comparator=Comparator.GREATER,
        threshold=8.5,
        units="totals",
    )
    pair = _match(kalshi, other)
    assert pair.confidence is not MatchConfidence.IDENTICAL  # type: ignore[attr-defined]


def test_different_scheduled_times_are_blocked() -> None:
    """Two venues never disagree by hours about the same game; a rematch is a different game."""
    kalshi = _kalshi("KXMLBGAME-26AUG251945BALSTL-STL", "Baltimore vs St. Louis", "St. Louis")
    other = _poly(
        "0x666",
        "Baltimore Orioles vs. St. Louis Cardinals",
        "Baltimore Orioles",
        "St. Louis Cardinals",
        start=datetime(2026, 8, 27, 23, 45, tzinfo=UTC),
    )
    pair = _match(kalshi, other)
    assert pair.confidence is not MatchConfidence.IDENTICAL  # type: ignore[attr-defined]


def test_unstated_settlement_source_blocks_rather_than_passes() -> None:
    """ "No stated source" is exactly where two venues quietly settle on different numbers."""
    kalshi = _kalshi("KXMLBGAME-26AUG251945BALSTL-STL", "Baltimore vs St. Louis", "St. Louis")
    other = _poly(
        "0x777",
        "Baltimore Orioles vs. St. Louis Cardinals",
        "Baltimore Orioles",
        "St. Louis Cardinals",
        sources=(),
    )
    pair = _match(kalshi, other)
    assert pair.confidence is MatchConfidence.STRONG  # type: ignore[attr-defined]
    assert any("settlement source" in blocker for blocker in pair.blockers)  # type: ignore[attr-defined]


def test_unrelated_markets_are_not_returned_at_all() -> None:
    kalshi = _kalshi("KXBTCD-26AUG2317-T86749.99", "Bitcoin price on Aug 23?", "$86,750 or above")
    other = _poly("0x888", "Will Orlando City SC win on 2026-08-22?", "Yes", "No")
    assert MarketMatcher().match_all([kalshi], [other]) == []
