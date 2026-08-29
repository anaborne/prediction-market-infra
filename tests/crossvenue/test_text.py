"""Tests for cross-venue wording normalization.

The team-name cases are the exact labels both venues used on 2026-08-22, read off live
responses. Kalshi writes `"Chicago WS"` where Polymarket writes `"Chicago White Sox"`. They are
listed exhaustively for the ambiguous cities because those are the ones a city-prefix match alone
gets wrong, and getting one wrong means hedging the Cubs with the White Sox.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kalshi_bot.crossvenue.text import (
    extract_threshold,
    inverse_document_frequency,
    normalize_text,
    parse_datetime_phrase,
    parse_number,
    resolve_team_alias,
    teams_match,
    thresholds_equivalent,
    tokenize,
    weighted_overlap,
)


@pytest.mark.parametrize(
    ("kalshi_label", "other_label", "expected"),
    [
        # Two franchises in one city: the disambiguator must select, not merely tolerate.
        ("Chicago WS", "Chicago White Sox", True),
        ("Chicago WS", "Chicago Cubs", False),
        ("Chicago C", "Chicago Cubs", True),
        ("Chicago C", "Chicago White Sox", False),
        ("New York Y", "New York Yankees", True),
        ("New York Y", "New York Mets", False),
        ("New York M", "New York Mets", True),
        ("Los Angeles A", "Los Angeles Angels", True),
        ("Los Angeles A", "Los Angeles Dodgers", False),
        ("Los Angeles D", "Los Angeles Dodgers", True),
        # One franchise: the bare city, with a multi-word city that must not be read as a
        # disambiguator ("bay" is three letters and part of the name).
        ("St. Louis", "St. Louis Cardinals", True),
        ("Tampa Bay", "Tampa Bay Rays", True),
        ("Texas", "Texas Rangers", True),
        ("Kansas City", "Kansas City Royals", True),
        # Not compositional at all: the exception table, reached through a normalized key since
        # `normalize_text` strips the apostrophe.
        ("A's", "Athletics", True),
        ("A's", "Houston Astros", False),
        # Unrelated teams must not match on nothing.
        ("Baltimore", "Tampa Bay Rays", False),
    ],
)
def test_teams_match_across_venue_naming_conventions(
    kalshi_label: str, other_label: str, expected: bool
) -> None:
    assert teams_match(kalshi_label, other_label) is expected


def test_resolve_team_alias_splits_city_from_disambiguator() -> None:
    assert resolve_team_alias("Chicago WS") == ("chicago", "ws")
    assert resolve_team_alias("St. Louis") == ("st louis", None)
    # "bay" must stay part of the city rather than becoming a nickname initial.
    assert resolve_team_alias("Tampa Bay") == ("tampa bay", None)


def test_normalize_text_splits_hyphenated_words_but_not_signed_numbers() -> None:
    assert tokenize("Netflix runner-up movie") == ("netflix", "runner", "up", "movie")
    # A spread's minus sign is part of the value and must survive.
    assert "-1.5" in normalize_text("Spread: Inter Miami CF (-1.5)")


def test_normalize_text_folds_accents() -> None:
    assert "kru" in normalize_text("KRÜ Esports")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$100,000", 100_000.0),
        ("100k", 100_000.0),
        ("1.5M", 1_500_000.0),
        ("86749.99", 86_749.99),
        ("25 bps", 25.0),
    ],
)
def test_parse_number_reads_every_written_form(text: str, expected: float) -> None:
    assert parse_number(text) == pytest.approx(expected)


def test_extract_threshold_reads_comparator_level_and_units() -> None:
    assert extract_threshold("Will Bitcoin reach $100,000 in August?") == (
        "greater_equal",
        100_000.0,
        "usd",
    )


def test_thresholds_equivalent_bridges_strictness_and_one_tick() -> None:
    # Kalshi quotes `> 86749.99` for what another venue quotes as `>= 86750`. Same contract.
    assert thresholds_equivalent("greater", 86_749.99, "greater_equal", 86_750.0, tick=0.01)
    # Same number, different strictness, is a different contract.
    assert not thresholds_equivalent("greater", 86_750.0, "greater_equal", 86_750.0)
    # Opposite directions are never the same claim.
    assert not thresholds_equivalent("greater", 100.0, "less", 100.0)
    # A gap wider than one tick is a different strike.
    assert not thresholds_equivalent("greater", 86_749.99, "greater_equal", 86_760.0, tick=0.01)


def test_parse_datetime_phrase_recovers_kalshi_scheduled_start() -> None:
    rules = (
        "If St. Louis wins the Baltimore vs St. Louis professional baseball game originally "
        "scheduled for Aug 25, 2026 at 7:45 PM EDT, then the market resolves to Yes."
    )
    assert parse_datetime_phrase(rules) == datetime(2026, 8, 25, 23, 45, tzinfo=UTC)


def test_parse_datetime_phrase_returns_none_without_a_date() -> None:
    assert parse_datetime_phrase("resolves when the committee announces") is None


def test_inverse_document_frequency_ranks_rare_tokens_above_common_ones() -> None:
    documents = [
        ("bitcoin", "price", "spanberger"),
        ("bitcoin", "price"),
        ("bitcoin", "volume"),
        ("bitcoin", "close"),
    ]
    weights = inverse_document_frequency(documents)
    assert weights["spanberger"] > weights["bitcoin"]


def test_weighted_overlap_is_zero_for_an_empty_side() -> None:
    assert weighted_overlap((), ("a",), {}) == 0.0
